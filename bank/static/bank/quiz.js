/* Quiz runner. Practice answers are checked locally; progress is kept in localStorage.
   Large decks are split into blocks of 50 questions, each with its own report. */
(() => {
  const root = document.getElementById("quiz");
  if (!root) return;
  const D = root.dataset;
  const QUESTION_TYPE = D.questionType;
  // Progress and the "go back to quiz" reminder belong to the signed-in account: several
  // accounts can share one browser, and its storage is shared between them.
  const KEY = "gd:" + (D.userId ? `u${D.userId}:` : "") + D.key;
  const ACTIVE_KEY = `gd:activeQuiz:${D.userId}`;
  const AI_SETTINGS_KEY = `gd:settings:${D.userId}`;
  const AUTH = D.auth === "1";
  const BLOCK_SIZE = 50;
  const esc = s => String(s).replace(/[&<>\"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
  const answerImage = q => q.answer_image_url
    ? `<figure class="answer-image-figure"><img src="${esc(q.answer_image_url)}" alt="Answer image"></figure>` : "";
  const isUofKLabel = label => /^u\s*of\s*k$/i.test(String(label || "").trim());
  const csrf = () => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
  const url = (tpl, id) => tpl.replace("/0/", "/" + id + "/");
  const editUrl = id => {
    const target = new URL(url(D.edit, id), location.href);
    const next = new URL(location.href);
    next.searchParams.set("resume_question", String(id));
    target.searchParams.set("next", next.pathname + next.search + next.hash);
    return target.pathname + target.search;
  };

  let S = {a: {}, order: null, retry: null, retryBlock: 0, removed: []};
  try {
    // Progress saved before keys named the account moves to the first account that opens it.
    const legacy = "gd:" + D.key;
    if (D.userId && localStorage.getItem(KEY) === null && localStorage.getItem(legacy) !== null) {
      localStorage.setItem(KEY, localStorage.getItem(legacy));
      localStorage.removeItem(legacy);
    }
    S = JSON.parse(localStorage.getItem(KEY)) || S;
  } catch (e) {}
  if (!S.a) S.a = {};
  if (!S.order) S.order = null;
  if (!Array.isArray(S.retry)) S.retry = null;
  if (!Array.isArray(S.removed)) S.removed = [];
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) {} };
  const updateResumeActivity = () => {
    try {
      const active = JSON.parse(localStorage.getItem(ACTIVE_KEY) || "null");
      if (review || view !== "quiz" || !list.length) {
        if (active?.key === KEY) {
          localStorage.removeItem(ACTIVE_KEY);
          window.dispatchEvent(new Event("gd:resume-activity-change"));
        }
        return;
      }
      localStorage.setItem(ACTIVE_KEY, JSON.stringify({
        key: KEY,
        title: D.title,
        url: location.pathname + location.search + location.hash,
        back: D.back,
        updatedAt: Date.now(),
      }));
      window.dispatchEvent(new Event("gd:resume-activity-change"));
    } catch (error) {
      console.warn("Unable to update the active quiz reminder.", error);
    }
  };

  let Q = [], byId = {}, list = [], pos = 0, view = "quiz", busy = false, navOpen = false, blockNavOpen = false;
  let resultBlock = 0, showUnanswered = false, retryMode = false;
  const review = D.review === "1";
  const drafts = {};
  const editing = {};
  let reviewNotice = "";
  const tfPending = {};
  const cm = {};
  const nt = {};  // per-question personal note panel: {open, text, err, msg, busy}
  const notice = {};

  const dirtyDraftIds = () => Object.keys(drafts).filter(id => drafts[id].dirty);
  const hasUnsavedDrafts = () => dirtyDraftIds().length > 0;

  const blockCount = () => Math.ceil(list.length / BLOCK_SIZE);
  const blockStart = b => b * BLOCK_SIZE;
  const blockEnd = b => Math.min(blockStart(b) + BLOCK_SIZE, list.length);
  const currentBlock = () => Math.floor(pos / BLOCK_SIZE);
  const blockList = b => list.slice(blockStart(b), blockEnd(b));

  const status = id => {
    const question = byId[id];
    if (question && question.kind === "image" && question.image_answer_mode === "identify") {
      const targets = question.targets || [];
      const targetProgress = S.a[id] && S.a[id].targets || {};
      if (!targets.length) return "new";
      if (targets.every(target => ["known", "missed"].includes(targetProgress[target.id]?.grade))) {
        return targets.every(target => targetProgress[target.id].grade === "known") ? "ok" : "bad";
      }
      return targets.some(target => targetProgress[target.id]?.revealed) ? "rev" : "new";
    }
    const a = S.a[id];
    if (!a) return "new";
    if (a.revealed) return "rev";
    return a.res.correct ? "ok" : "bad";
  };

  const tally = ids => {
    const t = {ok: 0, bad: 0, rev: 0, new: 0};
    (ids || list).forEach(id => t[status(id)]++);
    return t;
  };

  const blockComplete = b => blockList(b).every(id => status(id) !== "new");

  async function post(u, payload) {
    const r = await fetch(u, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRFToken": csrf()},
      body: JSON.stringify(payload || {}),
    });
    let j = {};
    try { j = await r.json(); } catch (e) {}
    return {status: r.status, json: j};
  }

  function check(id, payload) {
    if (busy) return;
    const question = byId[id];
    const reveal = !!payload.reveal;
    const res = {explanation: question.explanation, note: question.note, correct: null};
    if (question.kind === "desc") {
      res.answer = question.answer;
    } else if (question.kind === "tf") {
      res.key = question.answer_key;
      if (!reveal) {
        res.per = Object.fromEntries(Object.entries(res.key).map(([optionId, correct]) =>
          [optionId, (payload.answers || {})[optionId] === correct]
        ));
        res.correct = Object.values(res.per).every(Boolean);
      }
    } else {
      res.correct_ids = question.correct_ids;
      if (!reveal) res.correct = res.correct_ids.includes(payload.selected);
    }
    S.a[id] = {payload, res, revealed: reveal};
    save();
    render();
    scrollToAnswerAction();
  }

  function revealImageTarget(questionId, targetId) {
    const progress = S.a[questionId] && S.a[questionId].targets || {};
    if (progress[targetId]?.revealed || busy) return;
    if (!S.a[questionId] || !S.a[questionId].targets) S.a[questionId] = {targets: {}};
    const target = byId[questionId].targets.find(item => String(item.id) === String(targetId));
    S.a[questionId].targets[targetId] = {
      ...(S.a[questionId].targets[targetId] || {}),
      revealed: true,
      answer: target.answer,
    };
    save();
    render();
  }

  function revealAllImageTargets(questionId) {
    const question = byId[questionId];
    if (!S.a[questionId] || !S.a[questionId].targets) S.a[questionId] = {targets: {}};
    const progress = S.a[questionId].targets;
    for (const target of question.targets || []) {
      if (!progress[target.id]?.revealed) {
        progress[target.id] = {
          ...progress[target.id],
          revealed: true,
          answer: target.answer,
        };
      }
    }
    save();
    render();
  }

  function gradeImageTarget(questionId, targetId, grade) {
    const progress = S.a[questionId] && S.a[questionId].targets || {};
    if (!progress[targetId]?.revealed || progress[targetId]?.grade || busy) return;
    S.a[questionId].targets[targetId].grade = grade;
    save();
    render();
    const targets = byId[questionId].targets || [];
    if (targets.length && targets.every(target =>
      ["known", "missed"].includes(S.a[questionId].targets[target.id]?.grade)
    )) {
      scrollToAnswerAction();
    }
  }

  async function toggleSave(id) {
    if (!AUTH) { notice[id] = "login"; render(); return; }
    if (byId[id].saved && byId[id].my_note && !confirm("This question has a note. Removing it from Saved also deletes the note. Continue?")) return;
    const r = await post(url(D.save, id), {});
    if (r.status === 401) notice[id] = "login";
    else if (r.json && typeof r.json.saved === "boolean") {
      byId[id].saved = r.json.saved;
      if (!r.json.saved) { byId[id].my_note = ""; delete nt[id]; }
    }
    render();
  }

  async function saveNote(id, text) {
    const n = nt[id] || (nt[id] = {});
    n.busy = true; n.err = ""; n.msg = ""; render();
    const r = await post(url(D.note, id), {text});
    n.busy = false;
    if (r.status === 401) notice[id] = "login";
    else if (r.json && typeof r.json.note === "string") {
      byId[id].my_note = r.json.note;
      byId[id].saved = r.json.saved;
      n.text = r.json.note;
      n.msg = r.json.note ? "✓ Note saved." : "Note deleted.";
    } else n.err = (r.json && r.json.error) || "Could not save the note.";
    render();
  }

  async function sendComment(id) {
    const c = cm[id] || (cm[id] = {});
    const text = (c.text || "").trim();
    if (!text) { c.err = "Please write a comment first."; render(); return; }
    const r = await post(url(D.comment, id), {text});
    if (r.status === 401) notice[id] = "login";
    else if (r.json && r.json.ok) { c.msg = "✓ Sent to the admins — thank you!"; c.text = ""; c.err = ""; }
    else c.err = (r.json && r.json.error) || "Could not send the comment.";
    render();
  }

  function blockNav() {
    if (blockCount() <= 1) return "";
    const activeBlock = view === "results" ? resultBlock : currentBlock();
    const toggle = `<button class="btn nav-toggle" data-act="toggle-block-nav" aria-expanded="${blockNavOpen}" title="${blockNavOpen ? "Hide block list" : "Show block list"}">${blockNavOpen ? "▾" : "▸"}</button>`;
    const summary = `<button class="btn ${activeBlock === currentBlock() ? "pri" : ""}" data-act="block" data-block="${activeBlock}">Block ${activeBlock + 1}</button>`;
    const resetBlock = `<button class="btn" data-act="reset-block" title="Reset this block progress">Reset block</button>`;
    const buttons = Array.from({length: blockCount()}, (_, b) => {
      const ids = blockList(b), t = tally(ids), active = b === activeBlock;
      const done = blockComplete(b);
      return `<button class="btn ${active ? "pri" : ""}" data-act="block" data-block="${b}">
        Block ${b + 1}<span class="mute"> · ${ids.length}</span>${done ? " ✓" : ""}
        <span class="mute"> (${t.ok}/${ids.length})</span></button>`;
    }).join(" ");
    const mcqTfNav = QUESTION_TYPE === "mcq_tf";
    const navStyle = mcqTfNav ? "" : ' style="margin:0 0 10px;justify-content:flex-start;gap:6px;flex-wrap:wrap"';
    return `<div class="row${mcqTfNav ? " quiz-block-nav" : ""}"${navStyle}>
      <span class="mute">Blocks:</span>${toggle}${blockNavOpen ? buttons : summary}${resetBlock}</div>`;
  }

  function render() {
    const currentQuestion = view === "quiz" ? byId[list[pos]] : null;
    // Saved questions and book chapters mix every question type in one quiz.
    root.dataset.questionType = ["saved", "book"].includes(QUESTION_TYPE) && currentQuestion?.kind === "desc"
      ? "descriptive" : QUESTION_TYPE;
    root.innerHTML = view === "results" ? results() : question();
    if (view === "quiz") {
      const ta = root.querySelector("textarea.cmt");
      if (ta) ta.addEventListener("input", e => { (cm[list[pos]] || (cm[list[pos]] = {})).text = e.target.value; });
      const noteText = root.querySelector("textarea.question-note-text");
      if (noteText) noteText.addEventListener("input", e => { (nt[list[pos]] || (nt[list[pos]] = {})).text = e.target.value; });
    }
    updateResumeActivity();
  }

  function scrollToCurrentQuestion() {
    const questionContent = root.querySelector(".quiz-question .stem") || root.querySelector(".quiz-question");
    if (questionContent) questionContent.scrollIntoView({behavior: "smooth", block: "start"});
  }

  function scrollToAnswerAction() {
    const action = root.querySelector(".quiz-controls .btn.pri");
    if (!action) return;
    const targetTop = Math.round(window.innerHeight * 0.68);
    const scrollTop = window.scrollY + action.getBoundingClientRect().top - targetTop;
    window.scrollTo({top: Math.max(0, scrollTop), behavior: "smooth"});
  }

  function chatGptLink(q, a, targets) {
    let responseLanguage = "en";
    try {
      responseLanguage = JSON.parse(localStorage.getItem(AI_SETTINGS_KEY) || "{}").aiLanguage === "ar"
        ? "ar" : "en";
    } catch (error) {
      console.warn("Could not read the ChatGPT response-language preference; using English.", error);
    }
    const lines = [
      "I am studying medicine with a self-study quiz. Help me understand the underlying topic, not just memorize the answer.",
      "Do not mention, identify, or promote the website or quiz platform this question came from.",
      "Explain clearly and educationally. If the supplied explanation is incomplete or appears inaccurate, point that out and give the accurate reasoning.",
      responseLanguage === "ar"
        ? "Write your entire response in Arabic. Keep medical terms in English in parentheses where useful."
        : "Write your entire response in English.",
    ];
    if (targets) {
      lines.push(
        "Question type: Image identification (labelled figure).",
        `Question: ${q.stem}`,
        "Labels and their answers:",
        ...targets.map(target => `${target.label}: ${target.answer || "No answer recorded."}`),
        "For each structure, explain how to recognize it in this figure and its key relations or features.",
        `Quiz explanation: ${q.explanation || "None provided."}`,
      );
    } else if (q.kind === "desc") {
      lines.push(
        "Question type: Descriptive.",
        `Question: ${q.stem}`,
        `Suggested answer: ${a.res.answer || q.answer || "No written answer was provided."}`,
        `Quiz explanation: ${a.res.explanation || q.explanation || "None provided."}`,
      );
    } else if (q.kind === "tf") {
      lines.push(
        "Question type: True/False statements.",
        `Question: ${q.stem}`,
        "For each statement, explain why it is true or false. For false statements, briefly give a corrected version.",
        "Statements and answer key:",
        ...q.options.map(option => {
          const truth = a.res.key[option.id] ? "TRUE" : "FALSE";
          const selected = a.payload.answers && a.payload.answers[option.id] !== undefined
            ? ` (I selected ${a.payload.answers[option.id] ? "True" : "False"})` : "";
          return `${option.letter}. ${option.text} — Correct answer: ${truth}${selected}`;
        }),
        `Quiz explanation: ${a.res.explanation || q.explanation || "None provided."}`,
      );
    } else {
      const correctIds = a.res.correct_ids || q.correct_ids || [];
      lines.push(
        "Question type: Multiple choice.",
        `Question: ${q.stem}`,
        "Explain why each option is correct or incorrect, and teach the concept being tested.",
        "Options and answer key:",
        ...q.options.map(option => {
          const correctness = correctIds.includes(option.id) ? "CORRECT" : "INCORRECT";
          const selected = a.payload.selected === option.id ? " (I selected this option)" : "";
          return `${option.letter}. ${option.text} — ${correctness}${selected}`;
        }),
        `Quiz explanation: ${a.res.explanation || q.explanation || "None provided."}`,
      );
    }
    if (a.res.note || q.note) lines.push(`Quiz note: ${a.res.note || q.note}`);
    if (q.image_url) {
      lines.push(`The question's image is being pasted into this chat (also at ${new URL(q.image_url, location.href).href}). Use it for visual details; if no image has arrived, ask me to paste it instead of guessing.`);
    }
    lines.push("This is for learning only, not for personal diagnosis or treatment.");
    const chatUrl = new URL("https://chatgpt.com/");
    chatUrl.searchParams.set("q", lines.join("\n\n"));
    if (!q.image_url) {
      return `<a class="btn" href="${esc(chatUrl.href)}" target="_blank" rel="noopener noreferrer">اتونس 😁 ↗</a>`;
    }
    return `<a class="btn" href="${esc(chatUrl.href)}" target="_blank" rel="noopener noreferrer" data-copy-image="${esc(q.image_url)}">اتونس 😁 ↗</a>
      <p class="mute image-chatgpt-hint" data-copy-status>The image is copied when you open ChatGPT: paste it there with Ctrl+V (or long-press → Paste).</p>`;
  }

  function feedback(q, a) {
    const r = a.res;
    if (q.kind === "desc") {
      if (a.revealed) {
        return `<div class="fb info">` +
          (r.explanation ? `<p><b>Explanation:</b> ${esc(r.explanation)}</p>` : "") +
          (r.note ? `<p class="note">Note: ${esc(r.note)}</p>` : "") +
          `<div class="ai-question-actions">${chatGptLink(q, a)}</div></div>`;
      }
      const result = r.correct ? "✓ Marked as known." : "✗ Marked to review.";
      return `<div class="fb ${r.correct ? "ok" : "bad"}"><b>${result}</b>` +
        (r.explanation ? `<p>${esc(r.explanation)}</p>` : "") +
        (r.note ? `<p class="note">⚠ ${esc(r.note)}</p>` : "") +
        `<div class="ai-question-actions">${chatGptLink(q, a)}</div></div>`;
    }
    let head;
    if (a.revealed) head = '<div class="fb info"><b>Answer shown.</b>';
    else if (r.correct) head = '<div class="fb ok"><b>✓ Correct.</b>';
    else head = '<div class="fb bad"><b>✗ Not quite.</b>';
    if (q.kind === "tf") {
      head += " Key: " + q.options.map(o => o.letter + "=" + (r.key[o.id] ? "T" : "F")).join(", ") + ".";
    } else {
      head += " Correct: " + q.options.filter(o => r.correct_ids.includes(o.id)).map(o => o.letter).join(" and ") + ".";
    }
    if (r.explanation) head += "<p>" + esc(r.explanation) + "</p>";
    if (r.note) head += '<p class="note">⚠ ' + esc(r.note) + "</p>";
    return head + `<div class="ai-question-actions">${chatGptLink(q, a)}</div></div>`;
  }

  // Personal note: hidden until asked for, so it never gives away the answer by accident.
  function noteBox(id) {
    const q = byId[id], n = nt[id] || {};
    const label = n.open ? "📝 Hide note ▴" : q.my_note ? "📝 Show note" : "📝 Add note";
    const btn = `<button class="btn${q.my_note ? " has-note" : ""}" data-act="note-toggle" aria-expanded="${!!n.open}">${label}</button>`;
    if (!n.open) return btn;
    if (!AUTH) return `${btn}<div class="cmtbox question-note"><p class="mute"><a href="${esc(D.login)}">Log in</a> to keep notes on questions.</p></div>`;
    const text = n.text ?? q.my_note ?? "";
    return `${btn}<div class="cmtbox question-note">
      <textarea class="question-note-text" rows="4" maxlength="5000" placeholder="Something to remember next time you meet this question…">${esc(text)}</textarea>
      <div class="row" style="margin-top:6px"><span class="mute">Only you see this. Questions with notes are kept in your Saved questions.</span>
        <span>${q.my_note ? `<button class="btn" data-act="note-delete" ${n.busy ? "disabled" : ""}>Delete</button> ` : ""}<button class="btn pri" data-act="note-save" ${n.busy ? "disabled" : ""}>${n.busy ? "Saving…" : "Save note"}</button></span></div>
      ${n.err ? `<div class="fb bad">${esc(n.err)}</div>` : ""}${n.msg ? `<div class="fb ok">${esc(n.msg)}</div>` : ""}</div>`;
  }

  function commentBox(id) {
    const c = cm[id] || {};
    const finish = `<button class="btn" data-act="finish" title="See your results for this block now">End block</button>`;
    const btn = `<button class="btn" data-act="cmtoggle">💬 Comment${c.open ? " ▴" : ""}</button>`;
    if (!c.open) return `${finish} ${btn}`;
    let inner;
    if (!AUTH) {
      inner = `<p class="mute"><a href="${esc(D.login)}">Log in</a> to send a comment to the admins.</p>`;
    } else {
      inner = `<textarea class="cmt" rows="3" maxlength="2000" placeholder="Spotted a mistake or something unclear? Tell the admins…">${esc(c.text || "")}</textarea>
        <div class="row" style="margin-top:6px"><span class="mute">Private — only the admins see this.</span>
        <button class="btn pri" data-act="cmsend">Send to admins</button></div>
        ${c.err ? `<div class="fb bad">${esc(c.err)}</div>` : ""}${c.msg ? `<div class="fb ok">${esc(c.msg)}</div>` : ""}`;
    }
    return `${finish} ${btn}<div class="cmtbox">${inner}</div>`;
  }

  function draftFor(id) {
    const q = byId[id];
    return drafts[id] || (drafts[id] = {
      kind: q.kind, stem: q.stem, answer: q.answer || "",
      label: q.label || "", explanation: q.explanation || "", note: q.note || "",
      is_lab: !!q.is_lab,
      is_in_sorting_bin: !!q.is_in_sorting_bin,
      is_in_review_bin: !!q.is_in_review_bin,
      delete_question: false,
      dirty: false,
      image_answer_mode: q.image_answer_mode || "identify",
      targets: (q.targets || []).map(target => ({...target})),
      options: q.options.map(o => ({text: o.text, is_correct: !!o.is_correct})),
    });
  }

  function reviewEditor(id, q) {
    const draft = draftFor(id);
    const imageIdentify = draft.kind === "image" && draft.image_answer_mode === "identify";
    const options = draft.kind === "desc" || imageIdentify ? "" : `<div class="review-edit-options"><strong>Options and answer key</strong>` +
      draft.options.map((o, i) => `<div class="review-edit-option">
        <label> ${String.fromCharCode(65 + i)}
          <textarea rows="2" data-field="option-text" data-index="${i}">${esc(o.text)}</textarea>
        </label>
        <label class="review-correct"><input type="checkbox" data-field="option-correct" data-index="${i}" ${o.is_correct ? "checked" : ""}> Correct</label>
        <button class="btn" type="button" data-act="remove-option" data-index="${i}" aria-label="Remove option ${String.fromCharCode(65 + i)}">Remove</button>
      </div>`).join("") +
      (draft.options.length < 8 ? '<button class="btn" type="button" data-act="add-option">+ Add option</button>' : "") +
      "</div>";
    const targets = imageIdentify ? `<div class="review-edit-options"><strong>Identification targets</strong>` +
      draft.targets.map((target, index) => `<div class="review-edit-option">
        <label>Label<input type="text" data-field="target-label" data-index="${index}" value="${esc(target.label)}"></label>
        <label>Answer<textarea rows="2" data-field="target-answer" data-index="${index}">${esc(target.answer)}</textarea></label>
        <label>X %<input type="number" min="0" max="100" step="0.01" data-field="target-x" data-index="${index}" value="${target.x_percent ?? ""}"></label>
        <label>Y %<input type="number" min="0" max="100" step="0.01" data-field="target-y" data-index="${index}" value="${target.y_percent ?? ""}"></label>
        <button class="btn" type="button" data-act="remove-target" data-index="${index}">Remove</button>
      </div>`).join("") +
      (draft.targets.length < 50 ? '<button class="btn" type="button" data-act="add-target">+ Add target</button>' : "") +
      "</div>" : "";
    const imageDetails = q.image_url ? `<div class="review-image-preview"><img src="${esc(q.image_url)}" alt="Question image"></div>` : "";
    const answerImageEditor = draft.kind === "desc"
      ? `${q.answer_image_url ? `<div class="review-image-preview"><img src="${esc(q.answer_image_url)}" alt="Answer image"></div>` : ""}
        <a class="btn" href="${esc(editUrl(id))}">Manage answer image</a>` : "";
    const imageTargetsEditor = draft.kind === "image" ? `<label>Image answer style<select data-field="image_answer_mode">
      <option value="identify" ${draft.image_answer_mode === "identify" ? "selected" : ""}>Identification targets</option>
      <option value="mcq" ${draft.image_answer_mode === "mcq" ? "selected" : ""}>Image multiple choice</option>
    </select></label>${targets}` : "";
    return `<section class="review-editor" aria-label="Edit question">
      ${draft.delete_question ? '<div class="fb bad">Marked for deletion · unsaved</div>' : ""}
      <label>Type<select data-field="kind"><option value="mcq" ${draft.kind === "mcq" ? "selected" : ""}>Multiple choice</option><option value="tf" ${draft.kind === "tf" ? "selected" : ""}>True / False</option><option value="desc" ${draft.kind === "desc" ? "selected" : ""}>Descriptive</option>${q.image_url ? `<option value="image" ${draft.kind === "image" ? "selected" : ""}>Image-dependent</option>` : ""}</select></label>
      <label>Question<textarea rows="3" data-field="stem">${esc(draft.stem)}</textarea></label>
      <label>Label<input type="text" maxlength="100" data-field="label" value="${esc(draft.label)}"></label>
      ${imageDetails}
      ${draft.kind === "desc" ? `<label>Answer<textarea rows="3" data-field="answer">${esc(draft.answer)}</textarea></label>${answerImageEditor}` : draft.kind === "image" ? imageTargetsEditor + options : options}
      <label><input type="checkbox" data-field="is_lab" ${draft.is_lab ? "checked" : ""}> Include in Labs</label>
      <label>Explanation<textarea rows="3" data-field="explanation">${esc(draft.explanation)}</textarea></label>
      <label>Note<textarea rows="2" data-field="note">${esc(draft.note)}</textarea></label>
      <div class="row review-edit-actions">
        <button class="btn pri" type="button" data-act="save-review">Save question</button>
        <button class="btn" type="button" data-act="cancel-edit">Cancel edits</button>
        <button class="btn" type="button" data-act="stage-delete">${draft.delete_question ? "Cancel delete mark" : "Delete question"}</button>
      </div>
      <div class="mute" role="status">Changes are staged. They are not saved until you choose Save question.</div>
    </section>`;
  }

  async function saveReviewQuestion(id) {
    const draft = drafts[id];
    if (!draft || busy) return false;
    if (draft.delete_question && !confirm("Permanently delete this question? This cannot be undone.")) return false;
    busy = true;
    reviewNotice = "";
    render();
    try {
      const response = await post(url(D.editApi, id), draft);
      if (response.status !== 200 || (!response.json.question && !response.json.deleted)) {
        reviewNotice = (response.json && response.json.error) || "Could not save this question.";
        return false;
      }
      const oldBinState = !!byId[id].is_in_sorting_bin;
      const oldReviewBinState = !!byId[id].is_in_review_bin;
      const oldLabState = !!byId[id].is_lab;
      if (response.json.deleted) {
        Q = Q.filter(q => q.id !== id);
        delete byId[id];
        list = list.filter(questionId => questionId !== id);
        delete drafts[id];
        delete editing[id];
        delete S.a[id];
        pos = Math.min(pos, Math.max(0, list.length - 1));
        reviewNotice = "Question deleted.";
        return true;
      }
      const updated = response.json.question;
      byId[id] = updated;
      delete S.a[id];
      save();
      delete drafts[id];
      delete editing[id];
      const binStateChanged = oldBinState !== !!updated.is_in_sorting_bin
        || oldReviewBinState !== !!updated.is_in_review_bin;
      const labStateChanged = oldLabState !== !!updated.is_lab;
      if (binStateChanged || labStateChanged) {
        Q = Q.filter(q => q.id !== id);
        delete byId[id];
        list = list.filter(questionId => questionId !== id);
        if (binStateChanged) delete S.a[id];
        pos = Math.min(pos, Math.max(0, list.length - 1));
        if (labStateChanged && !binStateChanged) {
          reviewNotice = updated.is_lab
            ? "Saved and moved to Labs."
            : "Saved and moved to the regular question tabs.";
        } else if (updated.is_in_review_bin) {
          reviewNotice = "Saved and moved to the review bin.";
        } else if (updated.is_in_sorting_bin) {
          reviewNotice = "Saved and moved to the sorting bin.";
        } else {
          reviewNotice = "Saved and restored to its original chapter.";
        }
      } else {
        reviewNotice = "Question saved.";
      }
      return true;
    } catch (error) {
      reviewNotice = "Could not save this question. Check your connection and try again.";
      return false;
    } finally {
      busy = false;
      render();
    }
  }

  async function saveAllReviewDrafts() {
    for (const id of dirtyDraftIds()) {
      if (!await saveReviewQuestion(Number(id))) return false;
    }
    return true;
  }

  function askUnsavedAction() {
    return new Promise(resolve => {
      const overlay = document.createElement("div");
      overlay.className = "review-exit-overlay";
      overlay.innerHTML = `<section class="review-exit-dialog" role="dialog" aria-modal="true" aria-labelledby="review-exit-title">
        <h2 id="review-exit-title">You have unsaved review changes</h2>
        <p>Save your edits and sorting-bin moves before leaving, discard them, or stay here.</p>
        <div class="review-exit-actions">
          <button class="btn pri" type="button" data-choice="save">Save &amp; leave</button>
          <button class="btn" type="button" data-choice="discard">Discard &amp; leave</button>
          <button class="btn" type="button" data-choice="stay">Stay in review</button>
        </div>
      </section>`;
      document.body.appendChild(overlay);
      const finish = choice => {
        overlay.remove();
        resolve(choice);
      };
      overlay.addEventListener("click", event => {
        if (event.target === overlay) finish("stay");
        const button = event.target.closest("[data-choice]");
        if (button) finish(button.dataset.choice);
      });
      overlay.addEventListener("keydown", event => {
        if (event.key === "Escape") {
          event.preventDefault();
          finish("stay");
          return;
        }
        if (event.key !== "Tab") return;
        const buttons = [...overlay.querySelectorAll("button")];
        const first = buttons[0], last = buttons[buttons.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      });
      overlay.querySelector('[data-choice="save"]').focus();
    });
  }

  async function leaveReview(destination) {
    if (hasUnsavedDrafts()) {
      const previousFocus = document.activeElement;
      const choice = await askUnsavedAction();
      if (choice === "stay") {
        if (previousFocus && typeof previousFocus.focus === "function") previousFocus.focus();
        return;
      }
      if (choice === "save" && !await saveAllReviewDrafts()) return;
      if (choice === "discard") {
        dirtyDraftIds().forEach(id => {
          delete drafts[id];
          delete editing[id];
        });
      }
    }
    window.location.assign(destination);
  }

  async function moveQuizQuestionToBin(id) {
    if (!confirm("Move this question out of the chapter and into the sorting bin? Its quiz answer/progress for this question will be removed, but your other quiz progress will be kept.")) return;
    try {
      const response = await post(url(D.sortApi, id), {});
      if (response.status !== 200 || !response.json.moved) {
        reviewNotice = response.json.error || "Could not move the question to the sorting bin.";
        render();
        return;
      }
      const question = byId[id];
      S.removed.push({
        id, stem: question.stem,
        block: Math.floor(pos / BLOCK_SIZE) + 1,
        status: status(id),
      });
      Q = Q.filter(item => item.id !== id);
      delete byId[id];
      list = list.filter(questionId => questionId !== id);
      S.order = (S.order || []).filter(questionId => questionId !== id);
      if (Array.isArray(S.retry)) {
        S.retry = S.retry.filter(questionId => questionId !== id);
        if (!S.retry.length) S.retry = null;
      }
      delete S.a[id];
      delete tfPending[id];
      pos = Math.min(pos, Math.max(0, list.length - 1));
      if (!list.length) {
        view = "results";
        resultBlock = 0;
      }
      reviewNotice = "Moved to sorting bin. Your remaining quiz progress is saved.";
      save();
    } catch (error) {
      reviewNotice = "Could not move the question to the sorting bin. Check your connection and try again.";
    }
    render();
  }

  async function moveQuizQuestionToReviewBin(id) {
    if (!confirm("Move this question to the review bin? It will be removed from the chapter and kept in the admin review queue.")) return;
    try {
      const response = await post(url(D.reviewBinApi, id), {});
      if (response.status !== 200 || !response.json.moved) {
        reviewNotice = response.json.error || "Could not move the question to the review bin.";
        render();
        return;
      }
      const question = byId[id];
      S.removed.push({
        id, stem: question.stem,
        block: Math.floor(pos / BLOCK_SIZE) + 1,
        status: status(id),
      });
      Q = Q.filter(item => item.id !== id);
      delete byId[id];
      list = list.filter(questionId => questionId !== id);
      S.order = (S.order || []).filter(questionId => questionId !== id);
      if (Array.isArray(S.retry)) {
        S.retry = S.retry.filter(questionId => questionId !== id);
        if (!S.retry.length) S.retry = null;
      }
      delete S.a[id];
      delete tfPending[id];
      pos = Math.min(pos, Math.max(0, list.length - 1));
      if (!list.length) {
        view = "results";
        resultBlock = 0;
      }
      reviewNotice = "Moved to the review bin. Your remaining quiz progress is saved.";
      save();
    } catch (error) {
      reviewNotice = "Could not move the question to the review bin. Check your connection and try again.";
    }
    render();
  }

  function movedQuestionStatus(item) {
    const labels = {ok: "answered correctly", bad: "answered incorrectly", new: "not answered", rev: "answer shown"};
    return labels[item.status] || "removed from scoring";
  }

  function handleReviewNavigation(event) {
    const link = event.target.closest("a[href]");
    if (!review || !link || link.getAttribute("href") === "#" || !hasUnsavedDrafts()) return;
    event.preventDefault();
    leaveReview(link.href);
  }

  function question() {
    if (!list.length) return `<div class="card"><h2>${Q.length ? "All caught up" : "No questions found"}</h2><p>${Q.length ? "No questions remain in this review." : "There are no questions for this question type and source."}</p>
      <a class="btn" href="${esc(D.back)}" data-review-back>← Back</a>
      ${review ? '<button class="btn pri" data-act="exit-save">Save &amp; exit review</button>' : ""}</div>`;
    const id = list[pos], q = byId[id];
    const imageIdentify = q.kind === "image" && q.image_answer_mode === "identify";
    const a = review || imageIdentify ? null : S.a[id], st = status(id), b = currentBlock();
    const ids = blockList(b), t = tally(ids);
    const localPos = pos - blockStart(b);
    const nav = ids.map((j, i) =>
      `<button class="${status(j)} ${j === id ? "cur" : ""}" data-act="go" data-p="${blockStart(b) + i}" aria-label="Question ${i + 1}" title="Question ${i + 1}">${i + 1}</button>`).join("");
    const navToggle = `<button class="btn nav-toggle" data-act="toggle-nav" aria-expanded="${navOpen}" title="${navOpen ? "Hide question list" : "Show question list"}">${navOpen ? "▾" : "▸"}</button>`;
    let body = "";
    // Every question with an image renders it once, in a column that stays in view (see questionLayout below).
    const markers = q.kind === "image" ? (q.targets || []).filter(target =>
      target.x_percent !== null && target.y_percent !== null
    ).map(target => `<span class="image-marker" style="left:${target.x_percent}%;top:${target.y_percent}%">${esc(target.label)}</span>`).join("") : "";
    const hasImageDimensions = q.image_width > 0 && q.image_height > 0;
    const imageStageStyle = hasImageDimensions
      ? ` style="--image-aspect-ratio:${q.image_width}/${q.image_height};--image-max-width:${78 * q.image_width / q.image_height}vh;--image-w-per-h:${q.image_width / q.image_height}"`
      : "";
    const figure = q.image_url ? `<figure class="quiz-image-figure">
        <div class="image-stage${hasImageDimensions ? " is-calibrated" : ""}"${imageStageStyle}><img src="${esc(q.image_url)}" alt="Image for question ${localPos + 1} of ${ids.length}" loading="eager">
          ${markers ? `<div class="image-markers" aria-hidden="true">${markers}</div>` : ""}</div>
        <figcaption><a href="${esc(q.image_url)}" target="_blank" rel="noopener">Open original image</a></figcaption>
      </figure>` : "";
    if (q.kind === "image") {
      const imageStem = `<div class="stem image-stem">${esc(q.stem)}</div>`;
      if (imageIdentify) {
        const targetProgress = S.a[id] && S.a[id].targets || {};
        const answers = (q.targets || []).map(target => {
          if (review) {
            return `<section class="image-target-answer"><div class="image-target-title"><strong>${esc(target.label)}</strong>
              <div class="image-answer">${esc(target.answer || "No answer recorded.")}</div></div></section>`;
          }
          const state = targetProgress[target.id] || {};
          const revealButton = `<button class="btn pri" data-act="reveal-image-target" data-target="${target.id}" ${busy || state.revealed ? "disabled" : ""}>Reveal answer</button>`;
          const gradeButtons = state.revealed && !state.grade
            ? `<div class="image-grade"><span class="mute">Did you identify it?</span><button class="btn pri" data-act="grade-image-target" data-target="${target.id}" data-grade="known">I identified it</button><button class="btn" data-act="grade-image-target" data-target="${target.id}" data-grade="missed">I missed it</button></div>`
            : state.grade
              ? `<span class="tag">${state.grade === "known" ? "Self-rated: identified" : "Self-rated: missed"}</span>` : "";
          const answer = state.revealed
            ? `<div class="image-answer">${esc(state.answer || "No answer recorded.")}</div>`
            : `<div class="image-answer image-answer-hidden" aria-label="Answer hidden"><span>Answer hidden</span></div>`;
          return `<section class="image-target-answer"><div class="image-target-title"><strong>${esc(target.label)}</strong>${answer}</div>
            ${state.revealed ? gradeButtons : revealButton}</section>`;
        }).join("");
        // One ChatGPT button per figure, covering every revealed label, instead of one per label.
        const revealedTargets = review ? [] : (q.targets || []).filter(target => targetProgress[target.id]?.revealed)
          .map(target => ({...target, answer: targetProgress[target.id].answer}));
        const askChatGpt = revealedTargets.length
          ? `<div class="ai-question-actions">${chatGptLink(q, {res: {explanation: q.explanation, note: q.note}, payload: {}}, revealedTargets)}</div>`
          : "";
        body = `${imageStem}<div class="image-answer-list"><h3>${review ? "Identification targets and answers" : "Identify the labelled structures"}</h3>${answers}
          ${review ? "" : `<button class="btn" data-act="reveal-all-image-targets" ${busy ? "disabled" : ""}>Reveal all answers</button>`}</div>${askChatGpt}`;
      } else {
        body = imageStem + q.options.map(o => {
          let cl = review && o.is_correct ? "ok" : "";
          if (a) {
            if (a.res.correct_ids.includes(o.id)) cl = "ok";
            else if (a.payload.selected === o.id) cl = "bad";
          }
          return `<button class="opt ${cl}" ${review || a ? "disabled" : ""} data-act="pick" data-id="${o.id}"><b>${o.letter}</b><span>${esc(o.text)}</span></button>`;
        }).join("");
      }
    } else if (q.kind === "mcq") {
      body = `<div class="quiz-answer-list">${q.options.map(o => {
        let cl = review && o.is_correct ? "ok" : "";
        if (a) {
          if (a.res.correct_ids.includes(o.id)) cl = "ok";
          else if (a.payload.selected === o.id) cl = "bad";
        }
        return `<button class="opt ${cl}" ${review || a ? "disabled" : ""} data-act="pick" data-id="${o.id}"><b>${o.letter}</b><span>${esc(o.text)}</span></button>`;
      }).join("")}</div>`;
    } else if (q.kind === "tf") {
      const pend = tfPending[id] || {};
      body = `<div class="quiz-answer-list quiz-answer-list-tf">${q.options.map(o => {
        const chosen = a && !a.revealed ? a.payload.answers[o.id] : pend[o.id];
        const btn = (v, lbl) => {
          let cl = "";
          if (a) {
            if (a.res.key[o.id] === v) cl = "ok";
            else if (!a.revealed && chosen === v) cl = "bad";
          } else if (chosen === v) cl = "sel";
          return `<button class="${cl}" ${review || a ? "disabled" : ""} data-act="tf" data-id="${o.id}" data-v="${v}">${lbl}</button>`;
        };
        const mark = a && !a.revealed ? (a.res.per[o.id] ? " ✓" : " ✗") : "";
        return `<div class="tf"><div class="t"><b>${o.letter}</b>${esc(o.text)}<span class="mute">${mark}</span></div>
                <div class="tfb">${btn(true, "True")}${btn(false, "False")}</div></div>`;
      }).join("")}</div>`;
    } else {
      body = a && a.res.answer !== undefined
        ? `<div class="flash-answer"><b>Answer</b><div>${esc(a.res.answer)}</div>${answerImage(q)}</div>` : "";
    }
    const allSet = q.kind === "tf" && q.options.every(o => (tfPending[id] || {})[o.id] !== undefined);
    const extra = review || imageIdentify ? "" : st === "new"
      ? (q.kind === "tf" ? `<button class="btn pri" ${allSet ? "" : "disabled"} data-act="tfcheck">Check answers</button> ` : "") +
        (q.kind === "desc"
          ? '<button class="btn pri" data-act="reveal">Show answer</button> <button class="btn" data-act="skip">Skip</button> '
          : '<button class="btn" data-act="reveal">Show answer</button> ')
      : (q.kind === "desc" && a && a.revealed
          ? '<button class="btn pri" data-act="grade" data-grade="known">I knew it</button> <button class="btn" data-act="grade" data-grade="missed">I missed it</button> '
        : "");
    const firstInBlock = localPos === 0;
    const lastInBlock = localPos === ids.length - 1;
    const star = review ? "" : `<button class="star ${q.saved ? "on" : ""}" data-act="save" title="Save this question">${q.saved ? "★ Saved" : "☆ Save"}</button>`;
    const edit = review
      ? `<button class="btn" data-act="toggle-edit">${editing[id] ? "Close editor" : "✎ Edit question"}</button>`
      : D.edit ? `<a class="btn" href="${esc(editUrl(id))}">✎ Edit</a>` : "";
    const loginNote = notice[id] === "login"
      ? `<div class="fb info"><a href="${esc(D.login)}">Log in</a> to save questions and send comments.</div>` : "";
    const finishLabel = lastInBlock ? "Finish block" : "";
    const descriptiveRated = q.kind === "desc" && (st === "ok" || st === "bad");
    const next = review
      ? (pos < list.length - 1 ? `<button class="btn pri" data-act="go" data-p="${pos + 1}">Next →</button>` : "")
      : descriptiveRated && pos < list.length - 1
      ? `<button class="btn pri" data-act="go" data-p="${pos + 1}">Next →</button>`
      : q.kind === "desc" && !descriptiveRated ? ""
      : lastInBlock
      ? `<button class="btn pri" data-act="finish">${finishLabel} →</button>`
      : `<button class="btn pri" data-act="go" data-p="${pos + 1}">Next →</button>`;
    const prev = review
      ? `<button class="btn" ${pos === 0 ? "disabled" : ""} data-act="go" data-p="${pos - 1}">← Prev</button>`
      : firstInBlock && b > 0
      ? `<button class="btn" data-act="block" data-block="${b - 1}">← Previous block</button>`
      : `<button class="btn" ${pos === 0 ? "disabled" : ""} data-act="go" data-p="${pos - 1}">← Prev</button>`;
    const retryBanner = retryMode
      ? `<div class="retry-mode"><div class="retry-mode-copy"><strong>Retry mode</strong><span>${list.length} questions from your missed or unanswered set</span></div><button class="btn" data-act="all-blocks">Return to full quiz</button></div>`
      : "";
    const scopeLabel = review ? "" : retryMode ? "" : ` · Block ${b + 1}/${blockCount()}`;
    const reviewAnswer = review ? `<div class="fb info review-answer"><b>Answer</b>
      ${q.kind === "desc" ? `${q.answer ? `<p>${esc(q.answer)}</p>` : !q.answer_image_url ? "<p>No answer recorded.</p>" : ""}${answerImage(q)}` :
      q.kind === "image" && q.image_answer_mode === "identify"
        ? `<p>${(q.targets || []).map(target => `${esc(target.label)}. ${esc(target.answer)}`).join("<br>") || "No targets recorded."}</p>`
        : `<p>${q.kind === "tf"
        ? q.options.map(o => `${esc(o.letter)}. ${o.is_correct ? "True" : "False"} — ${esc(o.text)}`).join("<br>")
        : q.options.filter(o => o.is_correct).map(o => `${esc(o.letter)}. ${esc(o.text)}`).join("<br>") || "No answer key recorded."}</p>`}
      ${q.explanation ? `<p><b>Explanation:</b> ${esc(q.explanation)}</p>` : ""}
      ${q.note ? `<p class="note">Note: ${esc(q.note)}</p>` : ""}</div>` : "";
    const descriptiveStudy = q.kind === "desc"
      ? `<section class="flash-study">
          <div class="flash-prompt">
            <div class="stem">${esc(q.stem)}</div>
          </div>
          ${review ? reviewAnswer : body || `<div class="flash-answer-placeholder">
            <span class="flash-seal" aria-hidden="true">✦</span>
            <strong>Answer held back</strong>
            <span>Build your own response, then reveal to compare.</span>
          </div>`}
        </section>`
      : "";
    const stagedBinState = drafts[id] ? drafts[id].is_in_sorting_bin : q.is_in_sorting_bin;
    const stagedReviewBinState = drafts[id] ? drafts[id].is_in_review_bin : q.is_in_review_bin;
    const binAction = review ? `<button class="btn ${stagedBinState ? "" : "warn"}" data-act="stage-bin">
      ${stagedBinState ? "↩ Restore to original chapter" : '<span class="quiz-action-full">Move to sorting bin</span><span class="quiz-action-short">Sort</span>'}
    </button>` : "";
    const reviewBinAction = review ? `<button class="btn ${stagedReviewBinState ? "" : "warn"}" data-act="stage-review-bin">
      ${stagedReviewBinState ? "↩ Restore to chapter" : '<span class="quiz-action-full">Move to review bin</span><span class="quiz-action-short">Review</span>'}
    </button>` : "";
    const quizBinAction = !review && D.canSort === "1"
      ? '<button class="btn warn" data-act="move-quiz-bin" aria-label="Move to sorting bin" title="Move to sorting bin"><span class="quiz-action-full">Move to sorting bin</span><span class="quiz-action-short">Sort</span></button>' : "";
    const quizReviewBinAction = !review && D.canSort === "1"
      ? '<button class="btn warn" data-act="move-quiz-review-bin" aria-label="Move to review bin" title="Move to review bin"><span class="quiz-action-full">Move to review bin</span><span class="quiz-action-short">Review</span></button>' : "";
    const binLabel = review && drafts[id] && stagedBinState !== q.is_in_sorting_bin
      ? `<span class="tag">${stagedBinState ? "Marked for sorting bin · unsaved" : "Marked to restore · unsaved"}</span>` : "";
    const reviewBinLabel = review && drafts[id] && stagedReviewBinState !== q.is_in_review_bin
      ? `<span class="tag">${stagedReviewBinState ? "Marked for review bin · unsaved" : "Marked to restore · unsaved"}</span>` : "";
    return `${blockNav()}
      ${retryBanner}
      <div class="row quiz-meta"><a class="btn quiz-back-link" href="${esc(D.back)}" data-review-back">← Back</a>
      <span class="quiz-meta-copy"><span class="quiz-meta-title">${esc(D.title)}</span>
        <span class="quiz-meta-stats">${review ? `Question ${pos + 1} of ${list.length}` : `${scopeLabel ? `${scopeLabel.replace(/^ · /, "")} · ` : retryMode ? "Retry set · " : ""}${t.ok} correct · ${t.bad} missed · ${t.new + t.rev} left`}</span>
      </span>
      ${review ? '<button class="btn pri quiz-exit-review" data-act="exit-save">Save &amp; exit review</button>' : ""}</div>
      ${reviewNotice ? `<div class="fb info" role="status">${esc(reviewNotice)}</div>` : ""}
      <div class="bar quiz-progress"><i class="g" style="width:${t.ok / ids.length * 100}%"></i><i class="r" style="width:${t.bad / ids.length * 100}%"></i></div>
      <div class="nav-wrap quiz-navigation">${navToggle}<div class="nav ${navOpen ? "" : "closed"}">${navOpen ? nav : ""}</div></div>
      <div class="card quiz-question${q.kind === "desc" ? ` flashcard descriptive-question${a && a.res.answer !== undefined ? " is-revealed" : ""}` : ""}${q.kind === "mcq" ? " quiz-question-mcq" : q.kind === "tf" ? " quiz-question-tf" : ""}${figure ? " quiz-question-has-image" : ""}"><div class="qh">
        <div class="quiz-question-context"><span class="qn quiz-position" aria-label="Question ${localPos + 1} of ${ids.length}"><span>${String(localPos + 1).padStart(2, "0")}</span><span class="quiz-position-divider">/</span><span class="quiz-position-total">${String(ids.length).padStart(2, "0")}</span></span>
          ${q.label ? `<span class="tag quiz-source-badge">${isUofKLabel(q.label) ? "U of K" : esc(q.label)}</span>` : ""}
          ${q.multi ? '<span class="tag">more than one defensible</span>' : ""}
          ${q.kind === "tf" ? '<span class="tag">True / False</span>' : ""}
          ${q.is_in_sorting_bin ? '<span class="tag">In sorting bin</span>' : ""}${q.is_in_review_bin ? '<span class="tag">In review bin</span>' : ""}${binLabel}${reviewBinLabel}
        </div>
        <div class="quiz-question-tools${!edit && !binAction && !reviewBinAction && !quizBinAction && !quizReviewBinAction ? " quiz-question-tools-solo" : ""}">
          ${edit}${star}
          ${(binAction || reviewBinAction || quizBinAction || quizReviewBinAction) ? `<div class="quiz-question-triage">${binAction}${reviewBinAction}${quizBinAction}${quizReviewBinAction}</div>` : ""}
        </div>
      </div>
        ${loginNote}
        ${questionLayout(figure, `${q.kind === "desc" ? descriptiveStudy : q.kind === "image" ? body : `<div class="stem">${esc(q.stem)}</div>${body}`}${a ? feedback(q, a) : ""}
        ${review && q.source_chapter ? `<p class="mute review-source">Original chapter: ${esc(q.source_chapter)}</p>` : ""}
          ${review ? reviewAnswer : ""}
          <div class="ctl quiz-controls${q.kind === "desc" ? " flash-controls" : ""}">${prev}<span class="${q.kind === "desc" ? "flash-actions" : ""}">${extra}${next}</span></div>`)}
        ${review && editing[id] ? reviewEditor(id, q) : ""}
        ${review ? "" : `<div class="cmtwrap quiz-comments">${commentBox(id)} ${noteBox(id)}</div>`}</div>`;
  }

  // Image questions: the figure stays in view (left column on wide screens, top on phones)
  // while the stem, answers, and feedback scroll beside or below it.
  function questionLayout(figure, content) {
    return figure
      ? `<div class="image-question-layout">${figure}<div class="image-question-side">${content}</div></div>`
      : content;
  }

  function results() {
    if (!list.length) {
      const moved = S.removed || [];
      return `<div class="card quiz-empty-report">
        <h2>Quiz complete</h2>
        <p>No questions remain in this quiz set.</p>
        ${moved.length ? `<div class="fb info">${moved.length} question${moved.length === 1 ? "" : "s"} moved to the sorting bin during this quiz and excluded from your score.</div>
          <ul class="w">${moved.map(item => `<li>${esc(item.stem)} <span>${movedQuestionStatus(item)}</span></li>`).join("")}</ul>` : ""}
        <a class="btn" href="${esc(D.back)}">← Back to chapter</a>
      </div>`;
    }
    const ids = blockList(resultBlock), t = tally(ids), answered = t.ok + t.bad;
    const identificationTargets = ids
      .filter(id => byId[id].kind === "image" && byId[id].image_answer_mode === "identify")
      .flatMap(id => (byId[id].targets || []).map(target => ({
        ...((S.a[id] && S.a[id].targets && S.a[id].targets[target.id]) || {}),
        label: target.label,
      })));
    const imageMcqs = ids.filter(id => byId[id].kind === "mcq");
    const imageTrueFalse = ids.filter(id => byId[id].kind === "tf");
    const imageDescriptive = ids.filter(id => byId[id].kind === "desc");
    const imageReport = QUESTION_TYPE === "image"
      ? `<div class="image-report">
          ${imageMcqs.length ? `<p><strong>Image MCQs:</strong> ${imageMcqs.filter(id => status(id) === "ok").length} correct · ${imageMcqs.filter(id => status(id) === "bad").length} wrong · ${imageMcqs.filter(id => ["new", "rev"].includes(status(id))).length} unanswered / revealed</p>` : ""}
          ${imageTrueFalse.length ? `<p><strong>Image True / False:</strong> ${imageTrueFalse.filter(id => status(id) === "ok").length} correct · ${imageTrueFalse.filter(id => status(id) === "bad").length} wrong · ${imageTrueFalse.filter(id => ["new", "rev"].includes(status(id))).length} unanswered / revealed</p>` : ""}
          ${imageDescriptive.length ? `<p><strong>Image descriptive:</strong> ${imageDescriptive.filter(id => status(id) === "ok").length} known · ${imageDescriptive.filter(id => status(id) === "bad").length} missed · ${imageDescriptive.filter(id => ["new", "rev"].includes(status(id))).length} unanswered / revealed</p>` : ""}
          <p><strong>Identification (self-assessed):</strong> ${identificationTargets.filter(target => target.grade === "known").length} identified · ${identificationTargets.filter(target => target.grade === "missed").length} missed · ${identificationTargets.filter(target => !target.grade).length} not self-rated</p>
        </div>` : "";
    const wrong = ids.filter(i => status(i) === "bad");
    const left = ids.filter(i => ["new", "rev"].includes(status(i)));
    const pct = ids.length ? Math.round(t.ok / ids.length * 100) : 0;
    const temporaryNumber = id => ids.indexOf(id) + 1;
    const li = (arr, title) => arr.length ? `<h3 class="quiz-report-list-title">${title} (${arr.length})</h3><ul class="w quiz-report-list">` +
      arr.map(i => `<li><a href="#" data-act="jump" data-id="${i}" aria-label="Question ${temporaryNumber(i)}">Q${String(temporaryNumber(i)).padStart(2, "0")}</a><span>${esc(byId[i].stem.slice(0, 120))}</span></li>`).join("") + "</ul>" : "";
    const leftList = left.length ? `<div class="row quiz-unanswered-heading">
        <h3>Unanswered or revealed (${left.length})</h3>
        <button class="btn" data-act="toggle-results-list">${showUnanswered ? "Hide unanswered / revealed" : "Show unanswered / revealed"}</button>
      </div>
      ${showUnanswered ? `<ul class="w quiz-report-list">` + left.map(i => `<li><a href="#" data-act="jump" data-id="${i}" aria-label="Question ${temporaryNumber(i)}">Q${String(temporaryNumber(i)).padStart(2, "0")}</a><span>${esc(byId[i].stem.slice(0, 120))}</span></li>`).join("") + "</ul>" : ""}` : "";
    const nextBlock = resultBlock < blockCount() - 1
      ? `<button class="btn pri" data-act="block" data-block="${resultBlock + 1}">Next block →</button>` : "";
    const allBlocks = retryMode
      ? '<button class="btn" data-act="all-blocks">Return to full quiz</button>' : "";
    const moved = S.removed || [];
    return `${blockNav()}<div class="card quiz-report${QUESTION_TYPE === "descriptive" ? " descriptive-report" : ""}"><div class="quiz-report-summary">${QUESTION_TYPE === "image" ? "" : `<div class="big quiz-score">${pct}%</div>`}
      <div><h2 class="quiz-report-title">${retryMode ? "Retry set report" : `Block ${resultBlock + 1} report`}</h2>
      ${QUESTION_TYPE === "image" ? imageReport : `<div class="quiz-report-stats">${t.ok} correct · ${t.bad} wrong · ${left.length} unanswered / revealed</div>`}</div>
      </div><div class="quiz-report-content">
      ${moved.length ? `<div class="fb info">${moved.length} question${moved.length === 1 ? "" : "s"} moved to the sorting bin during this quiz and excluded from scoring.</div>
        <ul class="w quiz-report-list">${moved.map(item => `<li>${esc(item.stem)} <span>${movedQuestionStatus(item)}</span></li>`).join("")}</ul>` : ""}
      <div class="bar quiz-report-progress"><i class="g" style="width:${t.ok / ids.length * 100}%"></i><i class="r" style="width:${t.bad / ids.length * 100}%"></i></div>
      ${li(wrong, "Missed")}${leftList}
      <div class="result-actions quiz-report-actions"><button class="btn" data-act="back">← Back to block</button>
      <div class="result-action-group quiz-report-action-group">${wrong.length + left.length ? '<button class="btn pri" data-act="retry">Retry missed &amp; unanswered</button>' : ""}
      ${nextBlock} ${allBlocks} <button class="btn" data-act="reset">Reset progress</button></div></div></div></div>`;
  }

  // ChatGPT links cannot carry an image, so copy it to the clipboard for the student to paste.
  function imageAsPng(src) {
    return fetch(src, {mode: "cors"}).then(response => {
      if (!response.ok) throw new Error(`Image request failed (HTTP ${response.status}).`);
      return response.blob();
    }).then(blob => blob.type === "image/png" ? blob : createImageBitmap(blob).then(bitmap => {
      const canvas = document.createElement("canvas");
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      canvas.getContext("2d").drawImage(bitmap, 0, 0);
      return new Promise((resolve, reject) => canvas.toBlob(png => png ? resolve(png) : reject(new Error("Could not convert the image.")), "image/png"));
    }));
  }

  root.addEventListener("click", async e => {
    const link = e.target.closest("a[data-copy-image]");
    if (!link) return;
    e.preventDefault();
    const status = link.parentElement.querySelector("[data-copy-status]");
    let copied = false;
    if (navigator.clipboard?.write && window.ClipboardItem) {
      try {
        // Passing the promise keeps the click's user activation, which Safari requires.
        await navigator.clipboard.write([new ClipboardItem({"image/png": imageAsPng(link.dataset.copyImage)})]);
        copied = true;
      } catch (error) {
        console.warn("Could not copy the question image for ChatGPT.", error);
      }
    }
    const opened = window.open(link.href, "_blank");
    if (opened) opened.opener = null;
    if (status) {
      status.textContent = copied
        ? "Image copied. Paste it into ChatGPT with Ctrl+V (or long-press → Paste)."
        : "Couldn't copy the image here. In ChatGPT, attach it from “Open original image”.";
      if (!opened) status.textContent += " Your browser blocked the new tab; tap the button again.";
    }
  });

  root.addEventListener("click", e => {
    const b = e.target.closest("[data-act]");
    if (!b || b.disabled) return;
    e.preventDefault();
    const id = list[pos], d = b.dataset;
    switch (d.act) {
      case "go": pos = +d.p; view = "quiz"; render(); scrollToCurrentQuestion(); break;
      case "block": {
        const target = +d.block;
        const ids = blockList(target);
        const first = ids.findIndex(i => status(i) === "new");
        pos = blockStart(target) + (first < 0 ? 0 : first);
        view = "quiz"; render(); scrollToCurrentQuestion(); break;
      }
      case "toggle-nav": navOpen = !navOpen; render(); break;
      case "toggle-block-nav": blockNavOpen = !blockNavOpen; render(); break;
      case "toggle-results-list": showUnanswered = !showUnanswered; render(); break;
      case "reset-block": {
        const target = view === "results" ? resultBlock : currentBlock();
        if (confirm("Clear progress for this block?")) {
          blockList(target).forEach(i => { delete S.a[i]; delete tfPending[i]; });
          save();
          pos = blockStart(target);
          view = "quiz";
          render();
        }
        break;
      }
      case "pick": check(id, {selected: +d.id}); break;
      case "tf": (tfPending[id] = tfPending[id] || {})[d.id] = d.v === "true"; render(); break;
      case "tfcheck": check(id, {answers: tfPending[id]}); break;
      case "reveal": check(id, {reveal: true}); break;
      case "reveal-image-target": revealImageTarget(id, d.target); break;
      case "reveal-all-image-targets": revealAllImageTargets(id); break;
      case "grade-image-target": gradeImageTarget(id, d.target, d.grade); break;
      case "grade": {
        const answer = S.a[id].res.answer;
        S.a[id] = {payload: {}, res: {answer, correct: d.grade === "known", explanation: S.a[id].res.explanation, note: S.a[id].res.note}, revealed: false};
        save();
        if (pos < blockEnd(currentBlock()) - 1) pos++;
        else { resultBlock = currentBlock(); view = "results"; }
        render();
        if (view === "quiz") scrollToCurrentQuestion();
        else window.scrollTo(0, 0);
        break;
      }
      case "skip":
        if (pos < blockEnd(currentBlock()) - 1) pos++;
        else { resultBlock = currentBlock(); view = "results"; }
        render();
        if (view === "quiz") scrollToCurrentQuestion();
        else window.scrollTo(0, 0);
        break;
      case "save": toggleSave(id); break;
      case "cmtoggle": (cm[id] = cm[id] || {}).open = !(cm[id] || {}).open; if (cm[id].open) cm[id].msg = ""; render(); break;
      case "cmsend": sendComment(id); break;
      case "note-toggle": {
        const n = nt[id] = nt[id] || {};
        n.open = !n.open; n.msg = ""; n.err = "";
        if (n.open) n.text = byId[id].my_note || "";
        render();
        if (n.open) root.querySelector("textarea.question-note-text")?.focus();
        break;
      }
      case "note-save": saveNote(id, (nt[id] && nt[id].text) || ""); break;
      case "note-delete": if (confirm("Delete your note on this question?")) saveNote(id, ""); break;
      case "finish": resultBlock = currentBlock(); view = "results"; render(); window.scrollTo(0, 0); break;
      case "back": view = "quiz"; pos = blockStart(resultBlock) + Math.max(0, blockList(resultBlock).findIndex(i => status(i) === "new")); render(); break;
      case "jump": pos = list.indexOf(+d.id); view = "quiz"; render(); scrollToCurrentQuestion(); break;
      case "retry": {
        const retryIds = blockList(resultBlock).filter(i => status(i) !== "ok");
        if (!retryIds.length) break;
        retryIds.forEach(i => { delete S.a[i]; delete tfPending[i]; });
        if (!retryMode) S.retryBlock = resultBlock;
        S.retry = retryIds;
        retryMode = true;
        list = retryIds.slice();
        save();
        pos = 0;
        resultBlock = 0;
        view = "quiz"; render(); break;
      }
      case "all-blocks": {
        const currentId = list[pos];
        retryMode = false;
        S.retry = null;
        list = S.order.slice();
        const fullPosition = list.indexOf(currentId);
        resultBlock = fullPosition >= 0
          ? Math.floor(fullPosition / BLOCK_SIZE)
          : Math.min(S.retryBlock || 0, blockCount() - 1);
        const first = blockList(resultBlock).findIndex(i => status(i) === "new");
        pos = fullPosition >= 0 ? fullPosition : blockStart(resultBlock) + (first < 0 ? 0 : first);
        save();
        view = "quiz"; render(); scrollToCurrentQuestion(); break;
      }
      case "reset":
        if (confirm("Clear your progress for this quiz?")) { S = {a: {}, order: null, retry: null, retryBlock: 0, removed: []}; save(); location.reload(); }
        break;
      case "toggle-edit":
        editing[id] = !editing[id];
        if (!editing[id] && !drafts[id]) delete editing[id];
        render();
        break;
      case "stage-bin": {
        const draft = draftFor(id);
        draft.is_in_sorting_bin = !draft.is_in_sorting_bin;
        if (draft.is_in_sorting_bin) draft.is_in_review_bin = false;
        draft.dirty = true;
        render();
        break;
      }
      case "stage-review-bin": {
        const draft = draftFor(id);
        draft.is_in_review_bin = !draft.is_in_review_bin;
        if (draft.is_in_review_bin) draft.is_in_sorting_bin = false;
        draft.dirty = true;
        render();
        break;
      }
      case "stage-delete": {
        const draft = draftFor(id);
        draft.delete_question = !draft.delete_question;
        draft.dirty = true;
        editing[id] = true;
        render();
        break;
      }
      case "save-review": saveReviewQuestion(id); break;
      case "exit-save": leaveReview(D.back); break;
      case "move-quiz-bin": moveQuizQuestionToBin(id); break;
      case "move-quiz-review-bin": moveQuizQuestionToReviewBin(id); break;
      case "cancel-edit":
        delete drafts[id];
        delete editing[id];
        render();
        break;
      case "add-option": {
        const draft = draftFor(id);
        if (draft.options.length < 8) {
          draft.options.push({text: "", is_correct: false});
          draft.dirty = true;
        }
        render();
        break;
      }
      case "remove-option": {
        const draft = draftFor(id);
        draft.options.splice(+d.index, 1);
        draft.dirty = true;
        render();
        break;
      }
      case "add-target": {
        const draft = draftFor(id);
        if (draft.targets.length < 50) {
          draft.targets.push({label: "", answer: "", x_percent: null, y_percent: null});
          draft.dirty = true;
        }
        render();
        break;
      }
      case "remove-target": {
        const draft = draftFor(id);
        draft.targets.splice(+d.index, 1);
        draft.dirty = true;
        render();
        break;
      }
    }
  });

  function updateReviewDraft(e) {
    if (!review || !e.target.matches("[data-field]")) return;
    const id = list[pos], draft = draftFor(id), field = e.target.dataset.field;
    draft.dirty = true;
    if (field === "kind") {
      draft.kind = e.target.value;
      if (draft.kind === "image" && !draft.targets.length) {
        draft.targets = [{label: "A", answer: "", x_percent: null, y_percent: null}];
      }
      if (draft.kind !== "desc" && draft.kind !== "image" && draft.options.length < (draft.kind === "mcq" ? 2 : 1)) {
        draft.options = [
          {text: "", is_correct: true},
          ...(draft.kind === "mcq" ? [{text: "", is_correct: false}] : []),
        ];
      }
      render();
    } else if (field === "image_answer_mode") {
      draft.image_answer_mode = e.target.value;
      if (draft.image_answer_mode === "identify" && !draft.targets.length) {
        draft.targets = [{label: "A", answer: "", x_percent: null, y_percent: null}];
      }
      if (draft.image_answer_mode === "mcq" && draft.options.length < 2) {
        draft.options = [{text: "", is_correct: true}, {text: "", is_correct: false}];
      }
      render();
    } else if (field === "label") {
      draft.label = e.target.value;
    } else if (field === "is_lab") {
      draft.is_lab = e.target.checked;
    } else if (field === "option-text") {
      draft.options[+e.target.dataset.index].text = e.target.value;
    } else if (field === "option-correct") {
      draft.options[+e.target.dataset.index].is_correct = e.target.checked;
    } else if (field.startsWith("target-")) {
      const target = draft.targets[+e.target.dataset.index];
      const property = {
        "target-label": "label",
        "target-answer": "answer",
        "target-x": "x_percent",
        "target-y": "y_percent",
      }[field];
      target[property] = e.target.value;
    } else {
      draft[field] = e.target.value;
    }
  }
  root.addEventListener("input", updateReviewDraft);
  root.addEventListener("change", updateReviewDraft);
  document.addEventListener("click", handleReviewNavigation, true);
  window.addEventListener("beforeunload", e => {
    if (hasUnsavedDrafts()) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  document.addEventListener("keydown", e => {
    if (view !== "quiz" || !list.length || e.metaKey || e.ctrlKey || e.altKey) return;
    if (/^(TEXTAREA|INPUT|SELECT)$/.test((e.target.tagName || ""))) return;
    const b = currentBlock(), start = blockStart(b), end = blockEnd(b) - 1;
    if (e.key === "ArrowRight" && pos < end) { pos++; render(); }
    else if (e.key === "ArrowLeft" && pos > start) { pos--; render(); }
  });

  fetch(D.api).then(r => r.json()).then(data => {
    Q = data.questions;
    Q.forEach(q => byId[q.id] = q);
    const ids = Q.map(q => q.id);
    if (!Array.isArray(S.order)) {
      S.order = ids.slice();
      if (D.shuffle === "1") for (let i = S.order.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [S.order[i], S.order[j]] = [S.order[j], S.order[i]];
      }
    } else {
      const retained = S.order.filter(id => byId[id]);
      const retainedSet = new Set(retained);
      S.order = retained.concat(ids.filter(id => !retainedSet.has(id)));
      Object.keys(S.a).forEach(id => {
        if (!byId[id]) delete S.a[id];
      });
    }
    if (Array.isArray(S.retry)) {
      S.retry = S.retry.filter(id => byId[id]);
      if (!S.retry.length) S.retry = null;
    }
    retryMode = Array.isArray(S.retry) && S.retry.length > 0;
    list = retryMode ? S.retry.slice() : S.order.slice();
    const requestedStart = list.indexOf(Number(D.startQuestion));
    if (requestedStart >= 0 && !review) {
      const resumedId = list[requestedStart];
      delete S.a[resumedId];
      delete tfPending[resumedId];
      save();
    }
    const first = list.findIndex(i => status(i) === "new");
    pos = requestedStart >= 0 ? requestedStart : first < 0 ? 0 : first;
    if (requestedStart >= 0) {
      const currentUrl = new URL(location.href);
      currentUrl.searchParams.delete("resume_question");
      history.replaceState(null, "", currentUrl.pathname + currentUrl.search + currentUrl.hash);
    }
    save();
    render();
  }).catch(() => { root.innerHTML = '<div class="fb error">Could not load questions.</div>'; });
})();
