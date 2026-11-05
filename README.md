# The Grand Dozen

A Django site for hosting exam-question decks.

```
Home: big subject blocks
 └─ Subject page: optional sub-subject blocks, then semester bands
     └─ Deck cards (one per lesson, e.g. "Haematology")
         └─ Quiz  ·  ★ Save  ·  💬 Comment to admins  ·  ✎ Edit (editors)
```

* **Subjects → optional sub-subjects → semesters → decks.** Anatomy starts with ordered Embryology, Histology, and Gross Anatomy sections; any subject can use its own sections. A subject without sections keeps the existing semester-to-deck layout. A deck is a lesson.
* **Quizzes** with instant feedback, explanations, progress saving, shuffle, retry-missed. Correct answers are checked **server-side**.
* **Mock exams** – logged-in users can save and edit deck-level or subject-level templates with a question count, shared question-type mix, and optional timer. Subject templates select chapters, assign each a percentage of the exam, and use a semester filter only to find chapters. Questions are randomized within each type and presented in type groups; answers stay hidden until submission. Answers remain in the browser during the exam and are sent together on submission, so refreshing or closing the page before submitting can lose them. MCQ/true-false are scored automatically; after submission, descriptive answers can be assessed with Gemini AI (optional) or manually, while Identify answers remain self-assessed. AI marking is opt-in and requests wait in a shared server-side queue when the 15-requests-per-minute quota is busy; the exam page shows progress and displays the results when ready. Users can save a completed exam's exact question set as an unanswered template, delete attempts, or create a revocable link for another user to take an independent attempt.
* **★ Saved questions** – logged-in users can save any question. The **Saved** section mirrors the same layout (subject blocks → semester bands → decks) and lets you practise just the saved questions.
* **💬 Comments** – logged-in users can send a private comment about a question. Editors get a **Comments inbox** (with an open-count badge in the nav), jump straight to the question's edit page, and can resolve comments.
* **Editor pages** (not the Django admin) – manage a deck's questions, edit or add a question, delete it.
* **Full site backup** – staff can download the whole site as one gzip file: accounts (password hashes, groups, permissions), subjects, chapters, questions, options, image references, the Library, saved questions, exam templates and attempts. Students' private notes and comments are left out. Importing a full backup first saves the current database as a restore point (the last five are kept, with notes and comments), then replaces everything in one transaction; any restore point can be downloaded or restored from the Backup page. Older questions-only backups are still only added, never replaced. Image files stay in Supabase Storage and are referenced, not embedded.
* **Bulk import** with subject, optional sub-subject, semester, and deck title, preview and validation, duplicate detection, and a copy-paste **AI formatting prompt**.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py makemigrations bank
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_subjects                          # creates 4 placeholder subjects – rename them in /admin/
python manage.py import_questions data/*.txt --subject Physiology --semester 1   # optional seed data
python manage.py runserver
```

Open http://127.0.0.1:8000/

After signing in, choose **Install app** to add The Grand Dozen to your device. Installation prompts require HTTPS in production (localhost works for development); on unsupported browsers, use the browser menu’s **Install app** or **Add to Home Screen** option.

## Local question format converter

To convert numbered MCQ/descriptive documents into the website import format, run this from the project folder on Windows:

```powershell
python tools\question_converter.py
```

Paste the source document, optionally enter a subject, semester, and deck title, then choose **Convert and validate**. The converter checks its output with the same parser used by the website importer. Copy the generated text into the website’s **Import** page; questions marked for manual review remain invalid until their answer is corrected.

## PostgreSQL

SQLite remains the default unless `DATABASE_URL` is set. For a fresh local PostgreSQL database in pgAdmin:

1. Under **Login/Group Roles**, create a login role such as `granddozen_app`; allow login, but do not make it a superuser.
2. Under **Databases**, create `grand_dozen` and set its owner to `granddozen_app`.
3. In the project PowerShell terminal, set the connection URL and initialize the empty schema:

```powershell
$env:DATABASE_URL = "postgresql://granddozen_app:URL_ENCODED_PASSWORD@127.0.0.1:5432/grand_dozen"
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_subjects
```

Use a different `DATABASE_URL` for PythonAnywhere, with a PostgreSQL host reachable from PythonAnywhere; `127.0.0.1` there would mean the PythonAnywhere machine, not your PC. Keep credentials in environment/WSGI configuration, never in Git. The current `db.sqlite3` remains untouched when PostgreSQL is selected.

## Deploying to Render

1. Push the deployable code to GitHub, then create a Render **PostgreSQL** database and a **Web Service** from that repository. Keep both in the same region.
2. Set the Web Service build command to `bash build.sh` and the start command to `gunicorn config.wsgi:application`.
3. In the Web Service environment, add `DATABASE_URL` by linking the database's **Internal Database URL**. Also set `DJANGO_SECRET_KEY` to a generated secret, `DJANGO_DEBUG` to `0`, and `DJANGO_ALLOWED_HOSTS` to the Render hostname (for example, `grand-dozen.onrender.com`). Set `DJANGO_CSRF_ORIGINS` to the matching HTTPS origin (for example, `https://grand-dozen.onrender.com`).
4. To enable image questions on Render, create a public `rawasi-question-images` bucket in Supabase Storage, then add `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `SUPABASE_STORAGE_BUCKET` to the Web Service environment. Set the bucket variable to `rawasi-question-images`; keep the service-role key secret. Production uploads use Supabase Storage; local `DEBUG`-only file uploads are disabled when `DJANGO_DEBUG=0`. Do not store uploads on Render Free's local filesystem.
5. Back up the production database before deploying schema migrations. `build.sh` installs dependencies, collects static files, and applies pending migrations. Then open the Render Shell and run `python manage.py createsuperuser` only for a fresh database that does not yet have an admin account. Import real question data when ready; local SQLite users/questions and demo cards are not uploaded.

To enable optional AI marking for descriptive exam answers, set `GEMINI_API_KEY` in the server environment. `GEMINI_MODEL` may be set to a supported Gemini model; it defaults to `gemini-2.5-flash`. The key stays server-side. Users choose AI or manual assessment after submission, and choosing AI sends descriptive questions, suggested answers, and responses to Google Gemini. When the shared quota is busy, the request waits in a persistent queue and the open exam page checks for completion; users can switch to manual assessment while waiting. Keep the exam page open until results appear.

Do not use the PostgreSQL URL from your local pgAdmin server: Render must use its own hosted database URL. Keep all secrets in Render's environment settings, not in Git.

> Keep the committed `bank/migrations/` files with each deployment; the build applies them automatically. Migrations add schema and do not delete existing question data. Still take a production database backup before deploying, especially when the database contains important question banks.

### Subjects
Subjects are ordinary database rows: **/admin/ → Subjects**. Rename the placeholders, change `order`, or add more. The home page shows one block per subject. Subject sub-subjects are managed at **/admin/ → Subject sections**; adding sections to a subject makes its subject page show them as numbered blocks. Their `order` determines the order shown.

### Who can do what
| Person | Can |
|---|---|
| Anyone | browse and take quizzes |
| Logged-in user (anyone can **Sign up**) | save questions, send comments |
| Editor | edit / add / delete questions, read comments, bulk-import |
| Superuser | everything, plus subjects, decks and users in `/admin/` |

To make an editor: `/admin/` → Users → add user → give permissions `bank | question | Can add / change / delete question` (or use a Group). Editors don't need "Staff status" for the site's own pages; they only need it to enter `/admin/`.

## Adding questions

**Bulk (recommended):** *Import* in the top nav.
1. Copy the **AI formatting prompt**, paste it with your raw questions into any AI chat.
2. On the Import page choose **Subject**, an optional **Sub-subject**, **Semester**, and the **Deck title** (typing an existing title adds to that deck; a new title creates a deck), paste the AI's output, **Preview**, fix flagged items, **Import**.

**One at a time:** open a deck → *Manage / edit questions* → *+ Add question*.

**Image questions:** open a deck and choose *Add image question*. During local development (`DEBUG=1`), images upload to the ignored `media/` folder on this computer and are served by Django's development server; they are not bundled for deployment. Production (`DEBUG=0`) uses Supabase Storage configured with the project URL and a server-side service-role key. Keep that key only in the hosting provider's secret environment settings; never put it in source code or browser JavaScript. The `rawasi-question-images` bucket must be public so students can view images. Upload an image once, then create one image-dependent question at a time. Choose identification targets (with optional image markers) or image MCQ. After saving, choose *Save & add another question for this image* to reuse the upload. Deleting one question keeps an image still used by another question. Image questions are excluded from plain-text deck and bin exports because those formats cannot preserve the image or target markers.

Command line: `python manage.py import_questions file.txt --subject Anatomy --section "Gross Anatomy" --semester 2 --deck "Thorax"`.
`python manage.py export_bank <deck-slug> > backup.txt` writes a deck back in the same format (also on each deck page: *Download as text*).

Before deploying a code update that might affect data, or before moving to a new database, use the staff-only **Backup** tab to download the full backup (`deca-shine-backup-<date>.json.gz`) and keep it somewhere private; it contains every account and answer key. To move to a new database: deploy, run `python manage.py createsuperuser`, sign in, and import the file from the Backup tab (type REPLACE to confirm). Accounts come back with their old passwords. Restore points are stored in the database itself, so download one before that database is deleted.

## The text format

```
SUBJECT: Physiology            (optional – or use the form fields)
SECTION: Gross Anatomy         (optional – for subjects with sections)
SEMESTER: 1                    (optional)
DECK: Haematology              (optional)

Q: The globin part of haemoglobin F consists of:
NO: 17                         (optional – number in your source)
A. 2 alpha and 2 beta chains
B. 2 alpha and 2 gamma chains
C. 2 beta and 2 gamma chains
ANSWER: B                      (several correct? "A, C" – any is accepted)
EXPLANATION: HbF is alpha2 gamma2.
NOTE: shown to students as a warning (ambiguous / incomplete source)

Q: Regarding erythrocytes:
TYPE: TF                       (each option is a statement)
A. They contain carbonic anhydrase.
B. They have mitochondria.
ANSWER: TF                     (one T/F per statement; "A=T, B=F" also works)

Q: Explain the resting membrane potential:
TYPE: DESC                     (a descriptive flashcard; no options)
ANSWER: The voltage difference across a cell membrane at rest.
```

Rules: each question starts with `Q:`; MCQ/TF options are `A.`–`H.` in order; descriptive questions use `TYPE: DESC` and a text `ANSWER:` without options. Anything the parser can't understand is reported with its line number in the preview — nothing is guessed. Duplicates are detected inside the batch and against the target deck and skipped by default.

## Layout

```
config/                  settings, urls
bank/models.py           Subject → SubjectSection → QuestionBank (deck) → Question → Option, exam templates and attempts
bank/parser.py           plain-text format <-> Python (no Django imports; has its own tests)
bank/views.py            pages, JSON API, editor pages, import, signup
bank/forms.py            question editor form + option formset, signup form
bank/static/bank/        quiz.js, exam.js, style.css
bank/templates/bank/     home, subject_detail, section_detail, bank_detail (deck), quiz, exam, saved_*, manage_deck,
                         question_edit, comments, import, login, signup
bank/management/         import_questions, export_bank, seed_subjects
data/*.txt               244 seed questions
```

## Tests

```bash
python -m unittest bank.test_parser     # parser only, no Django needed
python manage.py test                   # everything
```

## Deploying
Set `DJANGO_SECRET_KEY`, `DATABASE_URL`, `DJANGO_DEBUG=0`, `DJANGO_ALLOWED_HOSTS=yourdomain`, `DJANGO_CSRF_ORIGINS=https://yourdomain`; run `migrate` and `collectstatic`; serve with gunicorn behind nginx (or WhiteNoise / a PaaS). The PWA manifest and root-scoped service worker require HTTPS in production. Consider email verification or a CAPTCHA on sign-up if the site is public, since anyone can register and comment.

## Ideas for next steps
Semester- and subject-level mock exams · attempt history and weak-deck stats per user · spaced repetition on saved questions · comment notifications by email · a public "report this question" count shown to editors.
