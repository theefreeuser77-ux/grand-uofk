"""Whole-site backup: export every table the site uses, and replace the database with a backup.

Export covers accounts (with their passwords' hashes, groups and permissions), every subject,
deck, question, option, image reference, library book, saved question, exam template, exam
attempt and AI record. Students' private notes and comments are left out of downloads but kept
in the automatic restore points, so restoring one puts the site back exactly as it was.

Replacing first saves the current database as a restore point (a BackupSnapshot row, which is
never wiped), then empties every backed-up table and loads the file in one transaction: if
anything fails, the database is left untouched.
"""
import datetime
import gzip
import json
from contextlib import contextmanager

from django.apps import apps
from django.contrib.auth.models import Permission
from django.core import serializers
from django.core.management.color import no_style
from django.core.serializers.json import DjangoJSONEncoder
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

FORMAT = "deca-shine-full-backup"
VERSION = 1
MAX_BACKUP_BYTES = 100 * 1024 * 1024
KEEP_SNAPSHOTS = 5

# Load order: a model comes after the models it points at (cycles are fine because foreign
# keys are only checked when the transaction commits).
MODELS = [
    "auth.Group", "auth.User",
    "bank.Subject", "bank.SubjectSection", "bank.QuestionBank", "bank.Book", "bank.BookChapter",
    "bank.QuestionImage", "bank.Question", "bank.Option", "bank.ImageTarget", "bank.SavedQuestion",
    "bank.ExamTemplate", "bank.ExamTemplateChapter", "bank.ExamAttempt", "bank.AIExamAssessment",
    "bank.AIRequestQuota",
]
PRIVATE_MODELS = ["bank.QuestionNote", "bank.Comment"]
# Emptied on replace but never backed up: they would point at the old accounts.
CLEARED_ONLY = ["admin.LogEntry", "sessions.Session"]
PERMISSION_FIELDS = {"auth.user": "user_permissions", "auth.group": "permissions"}


class BackupError(ValueError):
    pass


class _Encoder(DjangoJSONEncoder):
    """Like Django's encoder, but keeps microseconds so restored timestamps are exact."""

    def default(self, o):
        if isinstance(o, datetime.datetime):
            return o.isoformat()
        if isinstance(o, datetime.time):
            return o.isoformat()
        return super().default(o)


def _model(label):
    return apps.get_model(label)


def _permission_key(permission):
    return [permission.content_type.app_label, permission.content_type.model, permission.codename]


def build_payload(include_private=False):
    """Return the backup as plain Python data (lists and dicts)."""
    labels = MODELS + (PRIVATE_MODELS if include_private else [])
    permissions = {p.pk: _permission_key(p) for p in Permission.objects.select_related("content_type")}
    records, counts = [], {}
    for label in labels:
        model = _model(label)
        rows = serializers.serialize("python", model._default_manager.order_by("pk"))
        field = PERMISSION_FIELDS.get(label.lower())
        for row in rows:
            if field:
                row["fields"][field] = [permissions[pk] for pk in row["fields"].get(field, [])]
        counts[label] = len(rows)
        records.extend(rows)
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": timezone.now().isoformat(),
        "includes_private": include_private,
        "counts": counts,
        "records": records,
    }


def encode(payload):
    raw = json.dumps(payload, cls=_Encoder, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    data = gzip.compress(raw, compresslevel=6)
    if len(data) > MAX_BACKUP_BYTES:
        raise BackupError("The backup is larger than the 100 MB limit.")
    return data


def decode(data):
    """Accept a gzip file or plain JSON. Returns the parsed payload (any format)."""
    if len(data) > MAX_BACKUP_BYTES:
        raise BackupError("The backup file is larger than the 100 MB limit.")
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError) as error:
            raise BackupError("The backup file is damaged (it could not be decompressed).") from error
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise BackupError("The file is not a backup (it is not valid JSON).") from error


def is_full_backup(payload):
    return isinstance(payload, dict) and payload.get("format") == FORMAT


def _deserialize(payload):
    """Validate a full backup and turn it into unsaved model objects, grouped by model."""
    if payload.get("version") != VERSION:
        raise BackupError("This backup was made by a newer version of the site.")
    records = payload.get("records")
    if not isinstance(records, list):
        raise BackupError("The backup has no records list.")
    allowed = {label.lower() for label in MODELS + PRIVATE_MODELS}
    permissions = {
        tuple(_permission_key(p)): p.pk for p in Permission.objects.select_related("content_type")
    }
    for record in records:
        if not isinstance(record, dict) or str(record.get("model", "")).lower() not in allowed:
            raise BackupError(f"The backup contains an unknown kind of record: {record.get('model')!r}.")
        field = PERMISSION_FIELDS.get(record["model"].lower())
        if field:
            keys = record.get("fields", {}).get(field, [])
            record["fields"][field] = [permissions[tuple(key)] for key in keys if tuple(key) in permissions]
    grouped = {label.lower(): [] for label in MODELS + PRIVATE_MODELS}
    try:
        for item in serializers.deserialize("python", records, ignorenonexistent=True):
            grouped[item.object._meta.label_lower].append(item)
    except Exception as error:  # DeserializationError, ValidationError, ...
        raise BackupError(f"The backup could not be read: {error}") from error
    return grouped


@contextmanager
def _keep_timestamps(models):
    """bulk_create would overwrite auto_now / auto_now_add fields with the current time."""
    changed = []
    for model in models:
        for field in model._meta.concrete_fields:
            for flag in ("auto_now", "auto_now_add"):
                if getattr(field, flag, False):
                    setattr(field, flag, False)
                    changed.append((field, flag))
    try:
        yield
    finally:
        for field, flag in changed:
            setattr(field, flag, True)


def _tables_to_clear():
    tables = []
    for label in MODELS + PRIVATE_MODELS + CLEARED_ONLY:
        model = _model(label)
        tables.append(model._meta.db_table)
        for m2m in model._meta.local_many_to_many:
            tables.append(m2m.remote_field.through._meta.db_table)
    return list(dict.fromkeys(tables))


@transaction.atomic
def _replace(grouped):
    with connection.cursor() as cursor:
        for statement in connection.ops.sql_flush(no_style(), _tables_to_clear(), allow_cascade=True):
            cursor.execute(statement)
    restored = [_model(label) for label in MODELS + PRIVATE_MODELS]
    counts = {}
    with _keep_timestamps(restored):
        for label in MODELS + PRIVATE_MODELS:
            items = grouped[label.lower()]
            model = _model(label)
            model._default_manager.bulk_create([item.object for item in items], batch_size=500)
            counts[label] = len(items)
            for m2m in model._meta.local_many_to_many:
                through = m2m.remote_field.through
                source = m2m.m2m_field_name()
                target = m2m.m2m_reverse_field_name()
                links = [
                    through(**{f"{source}_id": item.object.pk, f"{target}_id": pk})
                    for item in items for pk in item.m2m_data.get(m2m.name, [])
                ]
                through._default_manager.bulk_create(links, batch_size=1000)
    with connection.cursor() as cursor:
        models = restored + [m.remote_field.through for model in restored for m in model._meta.local_many_to_many]
        for statement in connection.ops.sequence_reset_sql(no_style(), models):
            cursor.execute(statement)
    connection.check_constraints()
    return counts


def save_snapshot(reason):
    """Save the whole current database (notes and comments included) as a restore point."""
    from .models import BackupSnapshot

    payload = build_payload(include_private=True)
    data = encode(payload)
    snapshot = BackupSnapshot.objects.create(reason=reason, data=data, size=len(data), counts=payload["counts"])
    old = BackupSnapshot.objects.order_by("-created_at", "-pk").values_list("pk", flat=True)[KEEP_SNAPSHOTS:]
    BackupSnapshot.objects.filter(pk__in=list(old)).delete()
    return snapshot


def replace_database(payload, reason):
    """Save a restore point, then replace every backed-up table with the payload.

    Returns (snapshot, counts). Raises BackupError and leaves the database unchanged if the
    payload is invalid or does not load.
    """
    grouped = _deserialize(payload)
    if not grouped["auth.user"]:
        raise BackupError("The backup has no user accounts, so nobody could log in after importing it.")
    if not any(item.object.is_staff and item.object.is_active for item in grouped["auth.user"]):
        raise BackupError("The backup has no active admin account, so nobody could manage the site after importing it.")
    snapshot = save_snapshot(reason)
    try:
        counts = _replace(grouped)
    except (IntegrityError, ValueError) as error:
        raise BackupError(f"The backup could not be loaded, so nothing was changed: {error}") from error
    return snapshot, counts
