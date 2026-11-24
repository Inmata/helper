"""按本群档案编号关联管理员放入的照片，固定参考图顺序与人物对应关系。"""
from dataclasses import replace
from pathlib import Path
import random
import re

from wxbot_framework.conversation.members import MemberError, name_key
from wxbot_framework.integrations.jev import JevError


PORTRAIT_ROOT = Path(__file__).resolve().parents[2] / ".runtime" / "群友照片"
MAX_REFERENCES = 16
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_PHOTO = r"(?:照片|相片|相册|档案照|图库|照片库|照片档案库)"
_WHICH = r"(?:哪[些几位个]|那些|谁|多少|几[位个张])"
_INVENTORY = re.compile(
    rf"(?:你|[照相]片库|档案库|相册|图库).{{0,22}}{_WHICH}.{{0,12}}{_PHOTO}"
    rf"|(?:档案库|照片库|相册|图库).{{0,18}}{_WHICH}"
    rf"|{_WHICH}.{{0,8}}(?:有|存了|收录了).{{0,3}}档案照")
_ROOM_CAST = re.compile(r"(?:本群|全群|群里|群友)(?:的|面|里)?(?:众人|全员|所有人|所有成员|大家|每个人|全体|们)"
                        r"|(?:众|全体)群友|(?:档案库|照片库|库里).{0,14}(?:所有|全部|全员|众人|每个|的人.{0,8}合[照影]|的人物.{0,8}合[照影])")
_CAST_RESTRICTION = re.compile(r"除[了外去]|不(?:用|要|画|含|包括)|别(?:画|用)|只(?:用|画|选|要)|挑|任选|随机|其中|几[位个人]|路人|不入镜")


def asks_portrait_list(text):
    return bool(re.search(r"库|相册|你.{0,8}(?:存|收|保存|手里有)", text)
                and _INVENTORY.search(text) and not re.search(r"画|生成|制作|发给|发来|发一|转发", text))


def wants_room_cast(text, room_aliases=(), *, judge=None):
    if judge is not None:
        question = {
            "instructions": "在这个群的生图服务中，现成的人脸素材来自本群人物照片库。如果请求泛指手头能取到素材的人，按本群有档案照的人理解，无需出现群名。用户是否让这批人在画面里一起出场？允许有单独点名的主角。",
            "true": "希望整批已有照片对应的人物出镜或一起做动作。范围可由亲昵称呼、集体代词、要求人人参与或现有素材来表达。只提可用照片、要求别漏人都仍属全员。",
            "false": "群友只是受众、发起者、题字或鸣谢对象。只画少数指定角色、另一个群体、说不清指谁，或要求筛选排除部分人，都不属于整群出镜。",
        }
        try:
            return judge.noul({"绘图要求": text.strip(), "当前群的别称": list(room_aliases)}, [question], timeout=2)[0] >= 0.8
        except (JevError, TimeoutError, OSError, ValueError):
            pass
    for alias in sorted(room_aliases, key=len, reverse=True):
        text = text.replace(alias, "本群")
    return bool(_ROOM_CAST.search(text) and not _CAST_RESTRICTION.search(text)
                and not re.search(r"隔壁群|别的群|其他群(?:的|里)|鸣谢|题字|署名|写上|观众留言", text))


def _name_pattern(name):
    left = r"(?<![a-z0-9_])" if name[0].isascii() and name[0].isalnum() else ""
    right = r"(?![a-z0-9_])" if name[-1].isascii() and name[-1].isalnum() else ""
    return left + re.escape(name) + right


class RoomPortraits:
    def __init__(self, room, candidates, max_bytes, room_aliases=()):
        self.room_aliases = room_aliases
        self.people = {}
        self._photos = {}
        self._names = {}
        directory = PORTRAIT_ROOT / room
        if not directory.is_dir():
            return
        by_id = {p["id"]: p for p in candidates}
        for folder in sorted(directory.iterdir()):
            number = folder.name.partition("-")[0]
            if not number.isascii() or not number.isdecimal() or not folder.is_dir():
                continue
            person = by_id.get(int(number))
            if not person or person["id"] in self._photos:
                continue
            photo = min((f for f in folder.iterdir() if f.suffix.lower() in PHOTO_SUFFIXES and f.is_file()
                         and 0 < f.stat().st_size <= max_bytes),
                        key=lambda f: (f.stem != "默认", f.name.casefold()), default=None)
            if photo is None:
                continue
            self.people[person["id"]] = person
            self._photos[person["id"]] = photo
            for label in (person["name"], *person["aliases"]):
                self._names.setdefault(name_key(label), set()).add(person["id"])

    def fallback_names(self, request):
        """整理服务故障时只自动选直说要画的人，其余含名请求请群友点明。"""
        if wants_room_cast(request, self.room_aliases):
            return "全部", ""
        names = sorted(self._names, key=len, reverse=True)
        if not names:
            return [], ""
        pattern = "|".join(map(_name_pattern, names))
        matches = re.findall(pattern, request, re.IGNORECASE)
        if not matches:
            return [], ""
        subjects = rf"(?:{pattern})(?:\s*(?:和|跟|与|、|以及)\s*(?:{pattern}))*"
        if request.strip() == "不用档案照" or request.rstrip().endswith("\n不用档案照"):
            return [], ""
        explicit = re.fullmatch(rf"用\s*({subjects})(?:的)?档案照[。！!]?", request.splitlines()[-1].strip(), re.I)
        if explicit:
            return re.findall(pattern, explicit[1], re.I), ""
        direct = re.fullmatch(rf"(?:请|帮我|给我|再)?\s*(?:画(?:一张|个|幅)?)?\s*({subjects})"
                              r"(?:的)?(?:合影|合照|肖像|漫画像|全身照|半身照|头像)?[。！!]?", request.strip(), re.I)
        if direct:
            return re.findall(pattern, direct[1], re.I), ""
        return [], "这张图要用谁的档案照？可以回「用某某的档案照」或「不用档案照」。"

    def portrait_id(self, label):
        try:
            ids = self._names.get(name_key(label), ())
        except MemberError:
            return None
        return next(iter(ids)) if len(ids) == 1 else None

    def apply(self, plan, names, *, request=""):
        selected = {}
        if names == "全部":
            if not self.people:
                return None, "本群档案库里还没有可用的人物照片。能发来要入镜的人的照片吗？"
            selected = {pid: person["name"] for pid, person in self.people.items()}
        else:
            for label in names:
                person_id = self.portrait_id(label)
                if person_id is None:
                    return None, "这个称呼没有唯一可用的档案照。要用哪位的照片？也可以直接引用一张。"
                selected.setdefault(person_id, label)
        slots = MAX_REFERENCES - len(plan.references)
        if len(selected) > slots:
            named = [pid for pid in selected if any(re.search(_name_pattern(label), request, re.I)
                     for label in (self.people[pid]["name"], *self.people[pid]["aliases"]))]
            if len(named) > slots:
                chosen = random.sample(named, slots)
            else:
                chosen = named + random.sample([pid for pid in selected if pid not in named], slots - len(named))
            selected = {pid: selected[pid] for pid in chosen}
        if not selected:
            return plan, ""
        paths = list(plan.references)
        notes = []
        for person_id, label in selected.items():
            person = self.people[person_id]
            paths.append(str(self._photos[person_id]))
            labels = [person["name"]]
            labels.extend(alias for alias in person["aliases"] if alias.casefold() in plan.instruction.casefold()
                          or alias.casefold() == label.casefold())
            notes.append(f"第{len(paths)}张是{' / '.join(labels)}")
        guide = "人物外貌参考：" + "；".join(notes) + "。按对应照片保留人物长相，姿势和场景按绘图要求。\n"
        return replace(plan, instruction=guide + plan.instruction[:2000 - len(guide)], references=tuple(paths)), ""
