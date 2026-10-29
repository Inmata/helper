"""重复回复检测：与自己刚说过的话太像时提示换角度。"""
import unittest

from wxbot_framework.conversation.repetition import too_similar, similarity


class SimilarityTests(unittest.TestCase):
    def test_same_opening_counts_as_repeat(self):
        recent = ("志远你今天改走国学大师路线了啊",)
        self.assertEqual(too_similar("志远你今天转发公众号挺上瘾啊", recent), recent[0])

    def test_shared_wording_counts_as_repeat(self):
        # 开头不同，但整句几乎一样（只换了称呼）。
        recent = ("刚问完裸奔又开始讲周易",)
        self.assertEqual(too_similar("才问完裸奔又开始讲周易", recent), recent[0])

    def test_different_reply_is_not_repeat(self):
        recent = ("志远你今天改走国学大师路线了啊", "凯恩要是能拿金球奖")
        self.assertIsNone(too_similar("美联储加息跟金球奖没关系", recent))

    def test_empty_or_punctuation_only_is_ignored(self):
        self.assertIsNone(too_similar("", ("志远你今天",)))
        self.assertIsNone(too_similar("。。。", ("志远你今天",)))
        self.assertIsNone(too_similar("嗯", ("嗯",)))  # 太短没有二元组，也不算重复

    def test_similarity_is_symmetric(self):
        self.assertAlmostEqual(similarity("abcdef", "abcxyz"), similarity("abcxyz", "abcdef"))

    def test_only_recent_five_are_compared(self):
        recent = tuple(["志远你今天最早那句"] + ["今天天气不错%d" % index for index in range(5)])
        self.assertIsNone(too_similar("志远你今天又来", recent))


if __name__ == "__main__":
    unittest.main()
