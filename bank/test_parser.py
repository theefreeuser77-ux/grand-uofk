"""Pure-Python tests (no Django needed):  python -m unittest bank.test_parser"""
import unittest
from pathlib import Path

from bank.parser import DESC, MCQ, TF, parse, to_text

GOOD = """BANK: Demo
Q: Which is correct?
NO: 5
LABEL: U of K
A. one
B. two
C. three
ANSWER: b
EXPLANATION: because
  it wraps.
NOTE: source was messy

Q: Regarding RBCs:
TYPE: TF
A. Have a nucleus.
B. Contain carbonic anhydrase.
ANSWER: F, T
"""


class ParserTests(unittest.TestCase):
    def test_image_line_is_kept_with_its_question(self):
        question = parse("Q: What is the pathology shown?\nNO: 18\nIMAGE: 18.jpg\nTYPE: DESC\nANSWER: Fatty change\n").questions[0]
        self.assertEqual((question.image, question.kind, question.answer_raw), ("18.jpg", DESC, "Fatty change"))
        self.assertTrue(question.ok)

    def test_descriptive_flashcard_round_trip(self):
        text = "Q: Explain the resting membrane potential.\nTYPE: DESC\nANSWER: It is the voltage difference across the resting cell membrane.\n"
        question = parse(text).questions[0]
        self.assertTrue(question.ok, question.errors)
        self.assertEqual((question.kind, question.answer_raw), (DESC, "It is the voltage difference across the resting cell membrane."))
        exported = to_text("Neurophysiology", [{
            "number": "1", "kind": DESC, "stem": question.stem, "answer": question.answer_raw,
            "options": [], "explanation": "", "note": "",
        }])
        restored = parse(exported).questions[0]
        self.assertTrue(restored.ok, restored.errors)
        self.assertEqual((restored.kind, restored.stem, restored.answer_raw),
                         (DESC, question.stem, question.answer_raw))

    def test_redundant_question_number_is_removed_only_when_it_matches_no(self):
        matching = parse("Q: Q1 Name the two submucosal glands?\nNO: 1\nTYPE: DESC\nANSWER: Esophagus and duodenum\n").questions[0]
        mismatched = parse("Q: Q1 receptor is activated by the ligand.\nNO: 2\nTYPE: DESC\nANSWER: True\n").questions[0]
        self.assertEqual(matching.stem, "Name the two submucosal glands?")
        self.assertEqual(mismatched.stem, "Q1 receptor is activated by the ligand.")

    def test_inline_answer_explanation_is_parsed_as_separate_fields(self):
        question = parse(
            "Q: Name two parts of the GIT with submucosal glands?\n"
            "NO: 2\nTYPE: DESC\n"
            "ANSWER: Esophagus and duodenum Explanation: These contain the esophageal glands and Brunner's glands.\n"
        ).questions[0]
        self.assertEqual(question.answer_raw, "Esophagus and duodenum")
        self.assertEqual(question.explanation, "These contain the esophageal glands and Brunner's glands.")

    def test_inline_question_choices_are_extracted_and_classified(self):
        question = parse(
            "Q: Which artery supplies the lateral leg compartment? A- Anterior tibial "
            "B- Posterior tibial C- Dorsalis pedis D- Fibular (or peroneal)\n"
            "NO: 5\nLABEL: U of K\nTYPE: DESC\n"
            "ANSWER: D- Fibular (or peroneal)\n"
        ).questions[0]
        self.assertTrue(question.ok, question.errors)
        self.assertEqual(question.kind, MCQ)
        self.assertEqual(question.stem, "Which artery supplies the lateral leg compartment?")
        self.assertEqual(question.options, [
            "Anterior tibial", "Posterior tibial", "Dorsalis pedis", "Fibular (or peroneal)",
        ])
        self.assertEqual(question.correct, [False, False, False, True])
        self.assertEqual(question.label, "U of K")

    def test_inline_true_false_statements_are_extracted(self):
        question = parse(
            "Q: For each statement, mark whether it is true: A. Hallux has 3 phalanges "
            "B. Talus is largest bone\nTYPE: DESC\nANSWER: Neither (both false)\n"
        ).questions[0]
        self.assertTrue(question.ok, question.errors)
        self.assertEqual(question.kind, TF)
        self.assertEqual(question.options, ["Hallux has 3 phalanges", "Talus is largest bone"])
        self.assertEqual(question.correct, [False, False])

    def test_inline_choice_marker_does_not_split_hyphenated_choice_text(self):
        question = parse(
            "Q: Which statement is correct? A- The lateral meniscus is C-shaped and mobile "
            "B- The medial meniscus is more mobile\nANSWER: A- The lateral meniscus is C-shaped and mobile\n"
        ).questions[0]
        self.assertTrue(question.ok, question.errors)
        self.assertEqual(question.options[0], "The lateral meniscus is C-shaped and mobile")
        self.assertEqual(question.correct, [True, False])

    def test_inline_answer_can_mark_multiple_choices(self):
        question = parse(
            "Q: Which are true? A- First statement B- Second statement C- Third statement\n"
            "TYPE: DESC\nANSWER: A, C are True (first and third)\n"
        ).questions[0]
        self.assertTrue(question.ok, question.errors)
        self.assertEqual(question.kind, MCQ)
        self.assertEqual(question.correct, [True, False, True])
        self.assertEqual(question.answer_raw, "A, C")

    def test_basic_mcq_and_tf(self):
        r = parse(GOOD)
        self.assertEqual(r.bank_title, "Demo")
        a, b = r.questions
        self.assertTrue(a.ok and b.ok, (a.errors, b.errors))
        self.assertEqual((a.kind, a.number, a.correct), (MCQ, "5", [False, True, False]))
        self.assertEqual(a.label, "U of K")
        self.assertEqual(a.explanation, "because it wraps.")
        self.assertEqual(a.note, "source was messy")
        self.assertEqual((b.kind, b.correct), (TF, [False, True]))

    def test_multi_answer_accepts_lists(self):
        for ans in ("A, C", "A and C", "AC"[0] + "," + "C", "(a) (c)"):
            r = parse(f"Q: x\nA. 1\nB. 2\nC. 3\nANSWER: {ans}\n")
            self.assertEqual(r.questions[0].correct, [True, False, True], ans)

    def test_tf_answer_formats(self):
        for ans in ("TF", "T F", "true, false", "A=T, B=F", "a: true b: false"):
            r = parse(f"Q: x\nTYPE: TF\nA. p\nB. q\nANSWER: {ans}\n")
            self.assertEqual(r.questions[0].correct, [True, False], ans)

    def test_errors(self):
        cases = {
            "missing answer": "Q: x\nA. 1\nB. 2\n",
            "bad letter": "Q: x\nA. 1\nB. 2\nANSWER: D\n",
            "one option": "Q: x\nA. 1\nANSWER: A\n",
            "gap in letters": "Q: x\nA. 1\nC. 3\nANSWER: A\n",
            "tf wrong length": "Q: x\nTYPE: TF\nA. 1\nB. 2\nANSWER: T\n",
            "unknown type": "Q: x\nTYPE: essay\nA. 1\nB. 2\nANSWER: A\n",
            "empty option": "Q: x\nA. 1\nB.\nANSWER: A\n",
        }
        for name, txt in cases.items():
            self.assertFalse(parse(txt).questions[0].ok, name)

    def test_stray_and_unlabelled_lines(self):
        r = parse("Intro text\nQ: x\nA. 1\nB. 2\nANSWER: A\n\nrandom line\n")
        self.assertTrue(r.stray)
        self.assertFalse(r.questions[0].ok)  # unlabelled line after a blank is flagged

    def test_wrapped_stem_and_option(self):
        r = parse("Q: long stem\ncontinues here\nA. first\n  wrapped\nB. second\nANSWER: A\n")
        q = r.questions[0]
        self.assertEqual(q.stem, "long stem\ncontinues here")
        self.assertEqual(q.options[0], "first wrapped")
        self.assertTrue(q.ok)

    def test_duplicates_inside_batch(self):
        t = "Q: same\nA. 1\nB. 2\nANSWER: A\n\nQ: Same!\nA. 2\nB. 1\nANSWER: B\n"
        qs = parse(t).questions
        self.assertFalse(qs[0].duplicate_of)
        self.assertTrue(qs[1].duplicate_of)

    def test_seed_files_round_trip(self):
        files = sorted(Path(__file__).resolve().parent.parent.joinpath("data").glob("*.txt"))
        self.assertTrue(files)
        total = 0
        for f in files:
            txt = f.read_text(encoding="utf-8")
            r = parse(txt)
            total += len(r.questions)
            self.assertEqual([q for q in r.questions if q.errors], [], f.name)
            back = [dict(number=q.number, kind=q.kind, stem=q.stem,
                         options=list(zip(q.options, q.correct)),
                         explanation=q.explanation, note=q.note) for q in r.questions]
            self.assertEqual(to_text(r.bank_title, back), txt, f.name)
        self.assertEqual(total, 244)


if __name__ == "__main__":
    unittest.main()


class HeaderTests(unittest.TestCase):
    def test_subject_semester_deck_headers(self):
        r = parse("SUBJECT: Physiology\nSECTION: Gross Anatomy\nSEMESTER: Semester 2\nDECK: Haematology\n\nQ: x\nA. 1\nB. 2\nANSWER: A\n")
        self.assertEqual((r.subject, r.semester, r.bank_title), ("Physiology", 2, "Haematology"))
        self.assertEqual(r.section, "Gross Anatomy")
        self.assertTrue(r.questions[0].ok)

    def test_bank_alias_and_export_headers(self):
        r = parse("BANK: Old style\nQ: x\nA. 1\nB. 2\nANSWER: A\n")
        self.assertEqual(r.bank_title, "Old style")
        out = to_text("D", [], subject="Anatomy", semester=3)
        self.assertTrue(out.startswith("SUBJECT: Anatomy\nSEMESTER: 3\nDECK: D"))
        r2 = parse(out)
        self.assertEqual((r2.subject, r2.semester, r2.bank_title), ("Anatomy", 3, "D"))
        sectioned = to_text("D", [], subject="Anatomy", section="Histology", semester=3)
        self.assertEqual(parse(sectioned).section, "Histology")


class SortingBinMetadataTests(unittest.TestCase):
    def test_question_id_and_target_chapter_fields_are_parsed_per_question(self):
        text = """Q: Reorganized question
ID: 204
SOURCE_SUBJECT: Old subject
SOURCE_SEMESTER: 1
SOURCE_DECK: Old chapter
TARGET_SUBJECT: New subject
TARGET_SEMESTER: 3
TARGET_DECK: New chapter
A. First
B. Second
ANSWER: B
"""
        question = parse(text).questions[0]
        self.assertTrue(question.ok, question.errors)
        self.assertEqual(question.question_id, "204")
        self.assertEqual(
            (question.target_subject, question.target_semester, question.target_deck),
            ("New subject", "3", "New chapter"),
        )


class TopicRemovedTests(unittest.TestCase):
    def test_stray_topic_line_is_ignored_not_merged_into_the_question(self):
        r = parse("Q: x\nTOPIC: Blood\nA. 1\nB. 2\nANSWER: A\n")
        q = r.questions[0]
        self.assertTrue(q.ok)
        self.assertEqual(q.stem, "x")
        self.assertTrue(any("TOPIC" in w for w in q.warnings))
        self.assertFalse(hasattr(q, "topic"))
