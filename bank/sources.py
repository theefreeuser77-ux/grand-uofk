"""Where a question came from, read from its label.

Admins (staff) can filter by every university, plus "Additional" for questions without a
university label. Everyone else sees only "U of K" and "Other", so the other universities'
names stay private to admins.
"""
from django.db.models import Q

ALL = "all"
UOFK = "uofk"
OTHER = "other"
ADDITIONAL = "additional"

# (filter key, display name, labels that mean it; matched ignoring case)
UNIVERSITIES = [
    (UOFK, "U of K", ["U of K", "UofK", "U_of_K"]),
    ("gezira", "Gezira", ["Gezira"]),
    ("neelain", "Neelain", ["Neelain", "Al_Neelain", "Al Neelain", "Neelen"]),
    ("nile_valley", "Nile Valley", ["Nile_Valley", "Nile Valley"]),
    ("red_sea", "Red Sea", ["Red_Sea", "Red Sea"]),
]
_LABELS = {key: labels for key, _, labels in UNIVERSITIES}
_KEY_BY_LABEL = {label.lower(): key for key, _, labels in UNIVERSITIES for label in labels}

STUDENT_FILTERS = [ALL, UOFK, OTHER]
ADMIN_FILTERS = [ALL] + [key for key, _, _ in UNIVERSITIES] + [ADDITIONAL]

_STUDENT_NAMES = {ALL: "All questions", UOFK: "U of K only", OTHER: "Other questions"}
_ADMIN_NAMES = {ALL: "All questions", ADDITIONAL: "Additional", OTHER: "Other questions",
                **{key: name for key, name, _ in UNIVERSITIES}}


def _is_admin(user):
    return bool(getattr(user, "is_staff", False))


def _labels_q(labels):
    q = Q()
    for label in labels:
        q |= Q(label__iexact=label)
    return q


def _any_university_q():
    return _labels_q([label for labels in _LABELS.values() for label in labels])


def filter_q(key):
    """Q matching the questions a filter key selects (empty Q for "all")."""
    if key == ALL:
        return Q()
    if key == OTHER:
        return ~_labels_q(_LABELS[UOFK])
    if key == ADDITIONAL:
        return ~_any_university_q()
    return _labels_q(_LABELS[key])


def filters_for(user):
    """Filter keys this user may choose, in display order."""
    return ADMIN_FILTERS if _is_admin(user) else STUDENT_FILTERS


def normalize(value, user):
    """A filter key this user may use; anything else falls back to "all"."""
    allowed = set(filters_for(user))
    if _is_admin(user):
        allowed.add(OTHER)  # keeps older "other" links working for admins
    return value if value in allowed else ALL


def apply(queryset, key):
    return queryset if key == ALL else queryset.filter(filter_q(key))


def filter_name(key, user):
    return (_ADMIN_NAMES if _is_admin(user) else _STUDENT_NAMES)[key]


def source_of(label):
    """Filter key of the university a label names, or "additional"."""
    return _KEY_BY_LABEL.get((label or "").strip().lower(), ADDITIONAL)


def visible_label(label, user, from_book=False):
    """The label this user may see on a question.

    Admins see the stored label. Others see "U of K" on U of K questions and the book name on
    Library questions; other universities' names are hidden.
    """
    if _is_admin(user) or from_book:
        return label
    return "U of K" if source_of(label) == UOFK else ""
