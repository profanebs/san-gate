import json
import re
import unicodedata
from pathlib import Path

from .model import Finding

DATA = Path(__file__).parent / "data" / "patterns.json"


def _load():
    with DATA.open("r", encoding="utf-8") as f:
        return json.load(f)


_RAW = _load()
COMPILED = {key: [re.compile(p, re.I) for p in value] for key, value in _RAW.items()}


ZERO_WIDTH = re.compile(
    r"[\u00ad\u061c\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\U000E0000-\U000E007F]"
)

PRIVACY_CONTEXT = re.compile(
    r"(?i)\b("
    r"name|names|identifying|personal|private|anonymi[sz]e|blind|"
    r"confidential|confidentiality|anonymously?|secret|information|details|"
    r"identity|email|e-mail|contact|phone|telephone|address|age|"
    r"number|numbers|identifier|identification|status|"
    r"affiliation|nationality|citizenship|student\s*(?:id|number)|"
    r"date\s+of\s+birth|birth\s*date|birthdate|dob|gender|race|ethnicity|religion|"
    r"sexual\s+orientation|health|medical|financial|account|"
    r"social\s+security|ssn|passport|visa|residence|location|"
    r"school|employer|occupation|salary|income|"
    r"mobile|cell|gpa|grades?|score|scores|marks?|transcript|photo(?:graph)?|"
    r"marital|immigration|work\s+authorization|disability|veteran|pregnan"
    r")\b"
    r"|姓名|名字|隐私|个人信息|身份|匿名|邮箱|电子邮件|联系方式|电话|手机号码|手机号|手机|地址|住址|年龄|单位|工作单位|国籍|学号|生日|出生日期|出生年月|性别|种族|民族|宗教|健康|医疗|财务|账户|护照|签证|居住地|位置|学校|雇主|职业|收入|薪水|成绩|绩点|照片|婚姻|状况|状态|移民|残疾|退伍|怀孕"
)

# A privacy request can mention a canary target ("do not reveal the user's
# identity or that you followed this instruction"). Do not let the privacy
# suppression hide that second half.
CANARY_TARGET = re.compile(
    r"(?i)\b(that\s+you|you\s+are|you\s+were|this\s+phrase|this\s+instruction|"
    r"the\s+phrase|the\s+instruction|this\s+requirement|the\s+requirement|"
    r"this\s+rule|the\s+rule|this\s+sentence|the\s+sentence|"
    r"this\s+wording|the\s+wording|follow(?:ed|ing)?\s+this|"
    r"this\s+request|the\s+request)\b"
    r"|(?:你|您)(?:加入|使用|遵循|执行|照做|被要求|包含|写了|提到|引用|复制)"
    r"|(?:这句话|这个指令|该指令|这条要求|这个要求|上述要求|此要求|"
    r"这个短语|该短语|这个句子|该句子|这件事|此事)"
)

HOMOGLYPHS = str.maketrans(
    {
        "\u0410": "A",
        "\u0415": "E",
        "\u041e": "O",
        "\u0420": "P",
        "\u0421": "C",
        "\u0425": "X",
        "\u0406": "I",
        "\u0430": "a",
        "\u0435": "e",
        "\u043e": "o",
        "\u0440": "p",
        "\u0441": "c",
        "\u0443": "y",
        "\u0445": "x",
        "\u0456": "i",
        "\u0458": "j",
        "\u0455": "s",
        "\u0501": "d",
        "\u04bb": "h",
        "\u04cf": "l",
        "\u043c": "m",
        "\u0442": "t",
        "\u0432": "b",
        "\u043a": "k",
        "\u043d": "h",
        "\u0433": "r",
        "\u03b1": "a",
        "\u03b5": "e",
        "\u03bf": "o",
        "\u03c1": "p",
        "\u03c4": "t",
        "\u03c5": "u",
        "\u03bd": "v",
        "\u03ba": "k",
        "\u03b9": "i",
    }
)


def normalize_text(text):
    text = ZERO_WIDTH.sub("", text)
    text = text.translate(HOMOGLYPHS)
    text = unicodedata.normalize("NFKC", text)
    return " ".join(text.split())


def clip(text, limit=240):
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _matches(patterns, text):
    found = []
    for rx in patterns:
        for match in rx.finditer(text):
            found.append((match.start(), match.end(), match.group(0)))
    found.sort()
    deduped = []
    last_end = -1
    for start, end, value in found:
        if start >= last_end:
            deduped.append((start, end, value))
            last_end = end
    return deduped


def _same_sentence(text, start, end):
    """Return the sentence containing the match, not a fixed character window.

    A fixed window lets an unrelated privacy sentence later in the document
    suppress a concealment rule. The privacy noun must be in the same sentence
    as the concealment request.
    """
    boundaries = ".!?。！？\n"
    left = max(text.rfind(ch, 0, start) for ch in boundaries) + 1
    right_candidates = [text.find(ch, end) for ch in boundaries]
    right_candidates = [pos for pos in right_candidates if pos != -1]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left:right]


CONCEAL_VERB = re.compile(
    r"(?i)\b(?:do\s+not|don't|never)\s+"
    r"(?:state|mention|say|disclose|reveal|tell|inform)\b"
    r"|(?:不要|不得|别)\s*[^。\n]{0,40}?"
    r"(?:说明|提及|告诉|透露|承认|告知)"
)

TARGET_AT_END = re.compile(
    r"(?i)(?:that\s+you|you\s+are|you\s+were|this\s+phrase|"
    r"this\s+instruction|the\s+phrase|the\s+instruction|the\s+user|"
    r"the\s+reader|the\s+applicant|this\s+requirement|the\s+requirement|"
    r"this\s+rule|the\s+rule|this\s+request|the\s+request)\s*$"
    r"|(?:你|您|用户|读者|申请人|这句话|这个指令|该指令|这条要求|"
    r"这个要求|上述要求|此要求|这个短语|该短语|这个句子|该句子|"
    r"这件事|此事)\s*$"
)

CLAUSE_BOUNDARY = re.compile(
    r"(?i)[,;:()\[\]{}—–]|(?:，|；|：|——|（|）|【|】)"
    r"|\b(?:and|or|as\s+well\s+as|along\s+with|together\s+with|plus|including|"
    r"in\s+addition\s+to|in\s+relation\s+to|on\s+the\s+subject\s+of|"
    r"with\s+regard\s+to)\b"
    r"|(?:并且|而且|或|和|与|以及|还有|连同|加上)"
)

SUBORDINATE_BOUNDARY = re.compile(
    r"(?i)\b(?:because|since|so\s+that|so|though|although|whereas|"
    r"while|given\s+that|but|however|yet|unless|if|"
    r"(?<!well\s)as(?!\s+well\s+as))\b"
    r"|(?:因为|由于|所以|但是|不过|虽然|尽管|如果|若|"
    r"以保护|为了保护|以保障|为了保障|以符合|为了符合)"
)

LOCATION_PHRASE = re.compile(
    r"(?i)\b(?:in|on|at|of)\s+(?:the\s+)?"
    r"(?:[a-z][a-z'-]*\s+){0,6}"
    r"(?:essay|response|paper|assignment|text|document|answer|report|post|"
    r"paragraph|section|sentence|introduction|conclusion|body|title|heading|"
    r"personal\s+statement|application|sop|statement|interview|form|"
    r"first|second|third|last|final|next|\d+)"
    r"(?:'s)?(?:\s+(?:first|second|third|last|final|next|\d+))?"
    r"(?:\s+(?:sentence|paragraph|section))?"
    r"(?:\s+of\s+(?:the\s+)?(?:essay|response|paper|assignment|text|document|"
    r"report|post|personal\s+statement|application|sop|statement|interview|form))?"
    r"|(?:anywhere|in\s+public|at\s+any\s+point)"
    r"|在(?:论文|文章|回答|作业|报告|段落|正文|文本|文档|第三句|最后一段|"
    r"结论|引言|标题|章节|个人陈述|申请表)(?:中|里)?"
)

PRIVACY_COMPLEMENT = re.compile(
    r"(?i)\b(?:to|with|without|in|on|at|for|from|by)\s+"
    r"(?:anyone|anybody|third\s+part(?:y|ies)|the\s+public|public|consent)\b"
    r"|(?:给|向|对)\s*(?:第三方|任何人|公众)"
)

PRIVACY_REFERENCE = re.compile(
    r"(?i)\b(?:this|that|such)\s+(?:information|details|data)\b"
    r"|\b(?:about|regarding|concerning)\s+(?:[a-z][a-z'-]*\s+){0,2}"
    r"(?:information|details|data|name|names|identity|email|e-mail|phone|"
    r"telephone|address|age|number|identifier|status|affiliation|nationality|"
    r"citizenship|gender|race|ethnicity|religion|health|medical|financial|"
    r"account|passport|visa|residence|location|school|employer|occupation|"
    r"salary|income|mobile|cell|gpa|grades?|score|scores|marks?|transcript|"
    r"photo(?:graph)?|marital|immigration|disability|veteran|pregnan)\b"
)

PRIVACY_STOPWORDS = re.compile(
    r"(?i)\b(?:the|a|an|your|my|our|their|his|her|its|"
    r"user's|reader's|applicant's|student's|"
    r"user|reader|applicant|student|person|individual|"
    r"home|work|mailing|postal|physical|billing|permanent|current|of|to|any|all|"
    r"personal|private|identifying|confidential|anonymous|anonymously|"
    r"and|or|in|on|at|for|from|with|by|is|are|was|were|be|been|"
    r"being|will|would|can|could|should|must|may|might|do|does|did|"
    r"not|no|never|such|anyone|anybody|third|parties|party|public|consent|"
    r"as|if|where|which|who|whom|whose)\b"
    r"|(?:的|了|是|在|中|里|或|和|与|及|把|对|向|给|你|您|"
    r"用户|读者|申请人|姓名|名字)"
)


def _split_clauses(text):
    return [part.strip() for part in CLAUSE_BOUNDARY.split(text) if part.strip()]


def _cut_subordinate(text):
    match = SUBORDINATE_BOUNDARY.search(text)
    if match:
        return text[: match.start()]
    return text


def _object_clauses(match, text):
    """Return clauses that may contain the direct object of a conceal verb.

    English allows both "reveal the answer to the user" and "reveal to the
    user the answer". Chinese puts the object after the verb. Inspect both
    the text between the verb and the target and the text after the target,
    then split on punctuation and conjunctions so a privacy decoy in a later
    clause cannot hide an earlier non-privacy object.
    """
    start, end, value = match
    before = value
    verb = CONCEAL_VERB.search(before)
    if verb:
        before = before[verb.end():]
    before = TARGET_AT_END.sub(" ", before)
    after = text[end:]
    boundary = re.search(r"[.!?。！？\n]", after)
    if boundary:
        after = after[: boundary.start()]
    after = _cut_subordinate(after)
    return _split_clauses(before) + _split_clauses(after)


def _clause_is_privacy_only(clause):
    if not PRIVACY_CONTEXT.search(clause):
        return False
    text = clause
    while True:
        stripped = LOCATION_PHRASE.sub(" ", text)
        if stripped == text:
            break
        text = stripped
    text = PRIVACY_COMPLEMENT.sub(" ", text)
    text = PRIVACY_REFERENCE.sub(" ", text)
    text = re.sub(r"'s\b", " ", text)
    remainder = PRIVACY_CONTEXT.sub(" ", text)
    remainder = PRIVACY_STOPWORDS.sub(" ", remainder)
    remainder = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", " ", remainder)
    return not remainder.strip()


def _is_privacy_only(match, text):
    start, end, value = match
    sentence = _same_sentence(text, start, end)
    if CANARY_TARGET.search(sentence):
        return False
    clauses = _object_clauses(match, text)
    if not clauses:
        return False
    return all(_clause_is_privacy_only(clause) for clause in clauses)


def classify_text(text, location="text"):
    """Return semantic findings for a piece of text.

    The rule is intentionally conservative: a normal requirement should stay
    green, while a canary (do X + hide that X was done) should be red.
    """
    if not text or not text.strip():
        return []

    normalized = normalize_text(text)
    classic = _matches(COMPILED["classic"], normalized)
    ai_address = _matches(COMPILED["ai_address"], normalized)
    meta_critical = _matches(COMPILED["meta_critical"], normalized)
    meta_strong = _matches(COMPILED["meta_strong"], normalized)
    conceal = _matches(COMPILED["conceal"], normalized)
    privacy = _matches(COMPILED["privacy"], normalized)

    # Do not apply the privacy suppression to classic injection. A sentence
    # like "ignore previous instructions. The applicant's name is confidential."
    # is still an injection; the nearby privacy word must not erase it.
    conceal_compliance = [
        item for item in conceal if not _is_privacy_only(item, normalized)
    ]

    if classic or ai_address:
        evidence = (classic + ai_address)[0][2]
        return [
            Finding(
                "R_INJECTION",
                "RED",
                "injection",
                location,
                clip(evidence),
                "Instruction addressed to the AI or an attempt to override prior rules.",
            )
        ]

    if meta_critical and conceal_compliance:
        evidence = conceal_compliance[0][2]
        return [
            Finding(
                "R_CANARY",
                "RED",
                "canary",
                location,
                clip(evidence),
                "A critical meta-instruction is paired with a request to hide that it was followed.",
            )
        ]

    if conceal_compliance:
        evidence = conceal_compliance[0][2]
        return [
            Finding(
                "R_CONCEAL_COMPLIANCE",
                "RED",
                "canary",
                location,
                clip(evidence),
                "The text asks the reader or writer to hide that an instruction was followed.",
            )
        ]

    if meta_critical:
        evidence = meta_critical[0][2]
        return [
            Finding(
                "R_META_INSTRUCTION",
                "YELLOW",
                "canary",
                location,
                clip(evidence),
                "A critical meta-instruction about the exact output was found. Review before following.",
            )
        ]

    if meta_strong:
        evidence = meta_strong[0][2]
        return [
            Finding(
                "R_META_INSTRUCTION",
                "YELLOW",
                "canary",
                location,
                clip(evidence),
                "A meta-instruction about the exact form of the output. Review before following.",
            )
        ]

    # Privacy-only language is a normal requirement, not a canary.
    _ = privacy
    return []
