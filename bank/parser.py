"""
Plain-text question format -> structured data (and back).

This module has NO Django imports, so it can be tested and reused on its own.

Format (one question per block; blocks start with "Q:"):

    SUBJECT: Physiology                <- optional header lines (the import form can also set these)
    SECTION: Gross Anatomy             <- optional sub-subject
    SEMESTER: 1
    DECK: Haematology                  <- the lesson / deck title ("BANK:" is accepted as an alias)
    Q: The globin part of haemoglobin F consists of:
    NO: 17                             <- optional label (the number in your source)
    TYPE: MCQ                          <- optional: MCQ (default), TF, or DESC
    A. 2 alpha and 2 beta chains
    B. 2 alpha and 2 gamma chains
    C. 2 beta and 2 gamma chains
    ANSWER: B                          <- MCQ: letter(s) "B" or "A, C"; TF: "TFFTF"
    LABEL: U of K                      <- optional source label, kept separately from NOTE
    IMAGE: 17.jpg                      <- optional; images named after NO (17.jpg) match without it
    EXPLANATION: HbF = alpha2 gamma2.  <- optional
    NOTE: optional warning shown to the student (ambiguous / incomplete source)

    Q: Explain the resting membrane potential.
    TYPE: DESC
    ANSWER: The voltage difference across a cell membrane at rest.

Lines can wrap: text that follows a label line (Q, option, EXPLANATION, NOTE)
without a new label is treated as a continuation of it.
"""
import re
from dataclasses import dataclass, field

FIELD_RE = re.compile(
    r"^\s*(SUBJECT|SECTION|SEMESTER|DECK|BANK|Q|ID|SOURCE_SUBJECT|SOURCE_SEMESTER|SOURCE_DECK|"
    r"TARGET_SUBJECT|TARGET_SEMESTER|TARGET_DECK|NO|LABEL|TYPE|IMAGE|TOPIC|ANSWER|ANS|EXPLANATION|EXPL|NOTE)\s*:\s*(.*)$", re.I
)
OPT_RE = re.compile(r"^\s*\(?([A-Ha-h])[\.\)](?:\s+(.*))?$")
INLINE_OPT_RE = re.compile(r"(?<!\w)([A-Ha-h])\s*[-.)]\s+")
RULE_RE = re.compile(r"^\s*(-{3,}|={3,}|\*{3,})\s*$")

MCQ, TF, DESC = "mcq", "tf", "desc"
KIND_ALIASES = {
    "": MCQ, "mcq": MCQ, "sba": MCQ, "mc": MCQ, "single": MCQ, "multi": MCQ,
    "tf": TF, "t/f": TF, "truefalse": TF, "true/false": TF, "true-false": TF,
    "desc": DESC, "descriptive": DESC, "descriptivequestion": DESC,
    "flashcard": DESC, "shortanswer": DESC,
}


@dataclass
class ParsedQuestion:
    line: int
    stem: str = ""
    number: str = ""
    label: str = ""
    type_raw: str = ""
    answer_raw: str = ""
    explanation: str = ""
    note: str = ""
    question_id: str = ""
    image: str = ""                                  # IMAGE: file name of an image uploaded with the import
    target_subject: str = ""
    target_semester: str = ""
    target_deck: str = ""
    option_letters: list = field(default_factory=list)
    options: list = field(default_factory=list)      # option texts
    kind: str = MCQ
    correct: list = field(default_factory=list)      # list[bool] aligned with options
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    duplicate_of: str = ""                           # filled in by mark_duplicates()
    same_as: object = None                           # existing Question it repeats (merging.mark_existing)

    @property
    def ok(self):
        return not self.errors

    @property
    def key(self):
        return question_key(self.kind, self.stem, self.options,
                            answer=self.answer_raw if self.kind == DESC else "")


@dataclass
class ParseResult:
    questions: list = field(default_factory=list)
    bank_title: str = ""                              # the DECK title
    subject: str = ""
    section: str = ""
    semester: object = None                           # int or None
    stray: list = field(default_factory=list)        # (line_no, text) before the first Q:


def norm(s):
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def question_key(kind, stem, options, answer=""):
    """Order-insensitive fingerprint used for duplicate detection."""
    return kind + "|" + norm(stem) + "|" + "|".join(sorted(norm(o) for o in options)) + "|" + norm(answer)


def _strip_redundant_question_number(question):
    number = question.number.strip()
    if not number.isdigit():
        return
    match = re.match(r"^Q\s*0*(\d+)[.)\-:]?\s+(.+)$", question.stem, re.I)
    if match and int(match.group(1)) == int(number):
        question.stem = match.group(2)


def _extract_inline_options(question):
    if question.options:
        return
    matches = list(INLINE_OPT_RE.finditer(question.stem))
    if len(matches) < 2 or matches[0].group(1).upper() != "A":
        return
    letters = [match.group(1).upper() for match in matches]
    if letters != [chr(65 + index) for index in range(len(letters))]:
        return
    options = [
        question.stem[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(question.stem)].strip()
        for index, match in enumerate(matches)
    ]
    if any(not option for option in options):
        return

    answer = question.answer_raw.strip()
    expected = set(letters)
    tf_pairs = re.findall(r"(?<![A-Za-z])([A-H])\s*[:=\-]?\s+(true|false|t|f)\b", answer, re.I)
    if tf_pairs and {letter.upper() for letter, _ in tf_pairs} == expected:
        question.kind = TF
        question.type_raw = "TF"
        answers = {letter.upper(): value.lower().startswith("t") for letter, value in tf_pairs}
        question.correct = [answers[letter] for letter in letters]
        question.answer_raw = "".join("T" if answers[letter] else "F" for letter in letters)
    elif len(letters) == 2 and re.match(r"(?i)^neither\b", answer):
        question.kind = TF
        question.type_raw = "TF"
        question.correct = [False, False]
        question.answer_raw = "FF"
    else:
        selected = []
        marked_answers = list(INLINE_OPT_RE.finditer(answer))
        if marked_answers:
            for index, match in enumerate(marked_answers):
                letter = match.group(1).upper()
                value = answer[match.end():marked_answers[index + 1].start() if index + 1 < len(marked_answers) else len(answer)].strip()
                value = re.sub(r"\s+(?:or|and)\s*$", "", value, flags=re.I)
                if letter not in expected or norm(value) != norm(options[ord(letter) - 65]):
                    selected = []
                    break
                selected.append(letter)
        else:
            exact_matches = [letter for letter, option in zip(letters, options) if norm(answer) == norm(option)]
            if len(exact_matches) == 1:
                selected = exact_matches
            else:
                answer_letters = re.match(
                    r"^\s*([A-H](?:\s*(?:,|/|and|or)\s*[A-H])*)"
                    r"(?:\s+(?:are|is)\s+true\b|\s*\()",
                    answer,
                    re.I,
                )
                if answer_letters:
                    selected = re.findall(r"[A-H]", answer_letters.group(1).upper())
        if not selected or any(letter not in expected for letter in selected):
            return
        question.kind = MCQ
        question.type_raw = "MCQ"
        question.correct = [letter in selected for letter in letters]
        question.answer_raw = ", ".join(selected)

    question.stem = question.stem[:matches[0].start()].rstrip(" :")
    question.option_letters = letters
    question.options = options


def parse(text):
    res = ParseResult()
    cur, fld = None, None
    for n, raw in enumerate((text or "").replace("\r\n", "\n").split("\n"), 1):
        line = raw.rstrip()
        if RULE_RE.match(line):
            fld = None
            continue
        m = FIELD_RE.match(line)
        if m:
            key, val = m.group(1).upper(), m.group(2).strip()
            if key in ("BANK", "DECK"):
                res.bank_title = val or res.bank_title
                fld = None
            elif key == "SUBJECT":
                res.subject, fld = val, None
            elif key == "SECTION":
                res.section, fld = val, None
            elif key == "SEMESTER":
                num = re.search(r"\d+", val)
                res.semester, fld = (int(num.group()) if num else None), None
            elif key == "Q":
                cur = ParsedQuestion(line=n, stem=val)
                res.questions.append(cur)
                fld = "stem"
            elif key == "ID" and cur is not None:
                cur.question_id, fld = val, None
            elif key == "TARGET_SUBJECT" and cur is not None:
                cur.target_subject, fld = val, None
            elif key == "TARGET_SEMESTER" and cur is not None:
                cur.target_semester, fld = val, None
            elif key == "TARGET_DECK" and cur is not None:
                cur.target_deck, fld = val, None
            elif key in ("SOURCE_SUBJECT", "SOURCE_SEMESTER", "SOURCE_DECK"):
                fld = None
            elif cur is None:
                res.stray.append((n, line))
            elif key == "NO":
                cur.number, fld = val, None
            elif key == "LABEL":
                cur.label, fld = val, None
            elif key == "TYPE":
                cur.type_raw, fld = val, None
            elif key == "IMAGE":
                cur.image, fld = val, None
            elif key == "TOPIC":      # no longer used: the deck is the final classification
                cur.warnings.append("TOPIC line ignored (the deck is the only classification).")
                fld = None
            elif key in ("ANSWER", "ANS"):
                explanation = re.search(r"\s+Explanation:\s*", val, re.I)
                if explanation:
                    cur.answer_raw = val[:explanation.start()].rstrip()
                    cur.explanation = val[explanation.end():].strip()
                    fld = "explanation"
                else:
                    cur.answer_raw, fld = val, "answer"
            elif key in ("EXPLANATION", "EXPL"):
                cur.explanation, fld = val, "explanation"
            elif key == "NOTE":
                cur.note, fld = val, "note"
            continue
        if not line.strip():
            fld = None
            continue
        if cur is None:
            res.stray.append((n, line))
            continue
        om = OPT_RE.match(line)
        if om and fld in (None, "stem", "option"):
            cur.option_letters.append(om.group(1).upper())
            cur.options.append((om.group(2) or "").strip())
            fld = "option"
            continue
        txt = line.strip()
        if fld == "stem":
            cur.stem += "\n" + txt
        elif fld == "option":
            cur.options[-1] += " " + txt
        elif fld == "answer":
            cur.answer_raw += " " + txt
        elif fld == "explanation":
            cur.explanation += " " + txt
        elif fld == "note":
            cur.note += " " + txt
        else:
            cur.errors.append(
                f"Line {n} not recognised: “{txt[:50]}”. Put it under a label such as "
                "EXPLANATION: or directly beneath the line it continues."
            )
    for q in res.questions:
        _strip_redundant_question_number(q)
        _extract_inline_options(q)
        _validate(q)
    mark_duplicates(res.questions)
    return res


def _validate(q):
    q.stem = q.stem.strip()
    if not q.stem:
        q.errors.append("Question text is empty.")
    kind = KIND_ALIASES.get(q.type_raw.strip().lower().replace(" ", ""))
    if kind is None:
        q.errors.append(f"Unknown TYPE “{q.type_raw}” (use MCQ or TF).")
        kind = MCQ
    q.kind = kind

    if kind == DESC:
        if q.options:
            q.errors.append("Descriptive questions must not have options.")
        if not q.answer_raw.strip():
            q.errors.append("ANSWER is missing.")
        return

    expected = [chr(65 + i) for i in range(len(q.options))]
    if q.option_letters != expected:
        q.errors.append(
            "Options must be lettered A, B, C… in order with none missing "
            f"(found {', '.join(q.option_letters) or 'none'})."
        )
    for l, t in zip(q.option_letters, q.options):
        if not t:
            q.errors.append(f"Option {l} is empty.")
    if len(q.options) < (1 if kind == TF else 2):
        q.errors.append("Not enough options.")
    if len(q.options) > 8:
        q.errors.append("More than 8 options.")

    ans = q.answer_raw.strip()
    if not ans:
        q.errors.append("ANSWER is missing.")
    elif kind == MCQ:
        letters = [x.upper() for x in re.findall(r"(?<![A-Za-z])([A-Ha-h])(?![A-Za-z])", ans)]
        bad = [x for x in letters if x not in expected]
        if not letters:
            q.errors.append(f"ANSWER “{ans}” has no option letter.")
        elif bad:
            q.errors.append(f"ANSWER refers to option(s) {', '.join(bad)} that don't exist.")
        else:
            q.correct = [l in letters for l in expected]
    else:
        pairs = re.findall(r"([A-Ha-h])\s*[=:\-]\s*(true|false|t|f)\b", ans, re.I)
        if pairs:
            d = {a.upper(): v[0].upper() == "T" for a, v in pairs}
            if set(d) == set(expected):
                q.correct = [d[l] for l in expected]
        else:
            s = re.sub(r"(?i)true", "T", ans)
            s = re.sub(r"(?i)false", "F", s)
            s = re.sub(r"[\s,;/]+", "", s).upper()
            if re.fullmatch(r"[TF]+", s) and len(s) == len(expected):
                q.correct = [c == "T" for c in s]
        if not q.correct:
            q.errors.append(
                f"TF ANSWER must give one T/F per statement ({len(expected)} needed), e.g. "
                + "".join("TF"[i % 2] for i in range(len(expected))) + "."
            )
    if not q.explanation:
        q.warnings.append("No EXPLANATION.")
    if kind == MCQ and sum(q.correct) > 1:
        q.warnings.append("Several correct options — any of them will be accepted.")


def mark_duplicates(questions, existing_keys=None):
    """Flag questions that repeat an earlier one in the batch or an existing DB key."""
    seen = {}
    existing_keys = existing_keys or {}
    for q in questions:
        q.duplicate_of = ""
        if not q.stem:
            continue
        k = q.key
        if k in existing_keys:
            q.duplicate_of = f"already in bank ({existing_keys[k]})"
        elif k in seen:
            q.duplicate_of = f"same as Q{seen[k]} above"
        else:
            seen[k] = q.number or q.line


def to_text(deck_title, questions, subject=None, semester=None, section=None):
    """questions: iterable of dicts with question type, answer/options, explanation, and note."""
    out = []
    if subject:
        out.append(f"SUBJECT: {subject}")
    if section:
        out.append(f"SECTION: {section}")
    if semester:
        out.append(f"SEMESTER: {semester}")
    out += [f"DECK: {deck_title}", ""]
    for q in questions:
        out.append(f"Q: {q['stem']}")
        if q.get("number"):
            out.append(f"NO: {q['number']}")
        if q.get("label"):
            out.append(f"LABEL: {q['label']}")
        if q["kind"] == DESC:
            out += ["TYPE: DESC", f"ANSWER: {q['answer']}"]
        elif q["kind"] == TF:
            out.append("TYPE: TF")
        if q["kind"] != DESC:
            for i, (t, _) in enumerate(q["options"]):
                out.append(f"{chr(65 + i)}. {t}")
            if q["kind"] == TF:
                out.append("ANSWER: " + "".join("T" if c else "F" for _, c in q["options"]))
            else:
                out.append("ANSWER: " + ", ".join(chr(65 + i) for i, (_, c) in enumerate(q["options"]) if c))
        if q.get("explanation"):
            out.append(f"EXPLANATION: {q['explanation']}")
        if q.get("note"):
            out.append(f"NOTE: {q['note']}")
        out.append("")
    return "\n".join(out)


AI_PROMPT = """Reformat the exam questions I paste below into EXACTLY this plain-text format.
Output plain text only (no markdown, no code fences, no commentary).

Rules:
- Keep optional SUBJECT, SECTION, SEMESTER, and DECK header lines provided by me unchanged.
- Preserve a question's LABEL as its own "LABEL:" line; do not put it in NOTE.
- Every question starts with "Q:" followed by the question text.
- Options go on their own lines as "A. text", "B. text", … in order (max 8).
- Put "ANSWER:" after the options. Multiple choice: the correct letter(s), e.g. "ANSWER: B" or "ANSWER: A, C".
- True/False questions (each option is a statement): add "TYPE: TF" under the Q line and give one T or F per statement, e.g. "ANSWER: TFFTF".
- Descriptive questions: add "TYPE: DESC", omit options, and put the written answer after "ANSWER:".
- Image questions: an image question with options stays MCQ (or TF). An image question with one written
  answer is "TYPE: DESC" — e.g. "Identify this structure/tissue", "What is the pathology shown?",
  "What is the function of the labelled structure?". Keep the wording that refers to the image.
- If I gave an answer key use it. If not, work out the answer yourself. Exception: you cannot see the
  images, so if an image question has no answer given, leave "ANSWER:" empty and add "NOTE: Answer needed".
- Add "EXPLANATION:" with one or two sentences on why the answer is right.
- If the source is incomplete, ambiguous, or has a typo, add "NOTE:" describing the problem. Never invent missing options.
- Keep the original question number in "NO:" if there is one.
- Put a blank line between questions.

Example:
Q: The globin part of haemoglobin F consists of:
NO: 17
A. 2 alpha and 2 beta chains
B. 2 alpha and 2 gamma chains
C. 2 beta and 2 gamma chains
ANSWER: B
EXPLANATION: HbF is alpha2 gamma2.

Q: Regarding erythrocytes:
TYPE: TF
A. They contain carbonic anhydrase.
B. They have mitochondria.
ANSWER: TF
EXPLANATION: Mature RBCs have no mitochondria.

Q: Describe the resting membrane potential.
TYPE: DESC
ANSWER: The voltage difference across a cell membrane at rest.

Here are my questions:
"""


SORTING_BIN_AI_PROMPT = """Reorganize these questions into the exact same plain-text format.
Output plain text only: no markdown, code fences, or commentary.

This file contains questions from a sorting bin. Put each question into the chapter
where it belongs by filling in TARGET_SUBJECT, TARGET_SEMESTER, and TARGET_DECK for
EVERY question. Use the existing subject and chapter names provided by the user.

Critical rules:
- Preserve each question's ID exactly. It is how the website safely matches and updates it.
- Do not invent, remove, or duplicate IDs. Keep all questions.
- Keep SOURCE_SUBJECT, SOURCE_SEMESTER, and SOURCE_DECK unchanged.
- Never move a question to a different subject. TARGET_SUBJECT must exactly match that
  question's SOURCE_SUBJECT; you may only change its semester or deck within that subject.
- Use only destination decks listed for the question's SOURCE_SUBJECT.
- If SOURCE_SUBJECT is blank or Unfiled, do not guess a subject; leave target fields blank
  and add a NOTE for staff to decide where it belongs.
- You may correct wording, options, answer, explanation, note, and TYPE when necessary.
- Retain the original question format and answer-key rules from the standard prompt.
- Add TARGET_SUBJECT, TARGET_SEMESTER, TARGET_DECK after ID in each question.
- If you cannot confidently classify an item, leave target fields blank and add a NOTE.
- Each question starts with Q: and fields are per question, as in this example:

Q: Which object politely asks the clouds to wait?
ID: 123
SOURCE_SUBJECT: Bureau of Imaginary Weather
SOURCE_SEMESTER: 1
SOURCE_DECK: Forecasts for Indoors
TARGET_SUBJECT: Bureau of Imaginary Weather
TARGET_SEMESTER: 1
TARGET_DECK: Forecasts for Indoors
TYPE: MCQ
A. The velvet turnip
B. A cloud-shaped spoon
ANSWER: A
EXPLANATION: The chapter assigns this task to the velvet turnip.
"""
