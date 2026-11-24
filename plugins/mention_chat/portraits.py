"""按本群档案编号关联管理员放入的照片，固定参考图顺序与人物对应关系。"""
from dataclasses import replace
from pathlib import Path
import re

from wxbot_framework.conversation.members import MemberError, name_key


PORTRAIT_ROOT = Path(__file__).resolve().parents[2] / ".runtime" / "群友照片"
MAX_REFERENCES = 16
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class RoomPortraits:
    def __init__(self, room, candidates, max_bytes):
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
        names = sorted(self._names, key=len, reverse=True)
        if not names:
            return [], ""
        pattern = "|".join((r"(?<![a-z0-9_])" if n[0].isascii() and n[0].isalnum() else "")
                           + re.escape(n) + (r"(?![a-z0-9_])" if n[-1].isascii() and n[-1].isalnum() else "")
                           for n in names)
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

    def apply(self, plan, names):
        selected = {}
        for label in names:
            try:
                ids = self._names.get(name_key(label), ())
            except MemberError:
                ids = ()
            if len(ids) != 1:
                return None, "这个称呼没有唯一可用的档案照。要用哪位的照片？也可以直接引用一张。"
            selected.setdefault(next(iter(ids)), label)
        if len(plan.references) + len(selected) > MAX_REFERENCES:
            return None, f"这次参考图超过 {MAX_REFERENCES} 张了，要保留哪些人物或原图？"
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
