(() => {
  const root = document.getElementById("exam-runner");
  if (!root) return;

  const examPageTitle = document.title;
  const status = document.getElementById("exam-status");
  const questionElement = document.getElementById("exam-question");
  const navigation = document.getElementById("exam-navigation");
  const position = document.getElementById("exam-position");
  const clock = document.getElementById("exam-clock");
  const progressBar = document.getElementById("exam-progress-bar");
  const answeredCount = document.getElementById("exam-answered");
  const previous = document.getElementById("exam-previous");
  const next = document.getElementById("exam-next");
  const submit = document.getElementById("exam-submit");
  const state = { questions: [], allQuestions: [], current: 0, submitted: false, assessmentMode: false, reportQuestionReview: false, finalized: false, finishing: false };
  const markedQuestions = new Set();
  const ratingTimers = new Map();
  const ratingSaves = new Map();
  const noteState = new Map();  // question id -> {open, text, err, msg, busy}; notes appear only after submitting
  let countdown = null;
  let expiresAt = null;
  let aiPollTimer = null;
  let assessmentResult = null;
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.finalized) document.title = examPageTitle;
  });

  const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
  const csrf = () => {
    const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  };
  const getQuestion = () => state.questions[state.current];
  const examQuestionNumber = question => {
    const index = state.allQuestions.findIndex(item => item.id === question.id);
    return index >= 0 ? index + 1 : state.current + 1;
  };
  const fetchJson = async (url, options = {}) => {
    const response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrf(),
        ...(options.headers || {}),
      },
    });
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
      throw new Error(
        response.status >= 500
          ? "The server could not load your exam results. Please contact support if this continues."
          : "Your session may have expired. Refresh the page and sign in again."
      );
    }
    let data;
    try {
      data = await response.json();
    } catch {
      throw new Error("The server returned an invalid response. Please refresh the page and try again.");
    }
    if (!response.ok) throw new Error(data.error || "Unable to save the exam.");
    return data;
  };

  function optionMarkup(question, option, review) {
    const response = question.response || {};
    const selected = response.selected === option.id;
    const correct = review && question.correct_ids.includes(option.id);
    const bad = review && selected && !correct;
    return `<label class="exam-option ${selected ? "exam-selected" : ""} ${correct ? "exam-correct" : ""} ${bad ? "exam-incorrect" : ""}">
      <input type="radio" name="mcq-answer" value="${option.id}" ${selected ? "checked" : ""} ${review ? "disabled" : ""}>
      <b>${escapeHtml(option.letter)}</b><span>${escapeHtml(option.text)}</span>
    </label>`;
  }

  function reviewRating(question, key, label) {
    const rating = (question.response.ratings || {})[key];
    return `<div class="exam-assessment-choice">
      <span class="exam-assessment-caption">Self-assess:</span>
      <div class="exam-rating" role="group" aria-label="Self-assessment${label ? `: ${escapeHtml(label)}` : ""}">
        <button type="button" data-rating-key="${escapeHtml(key)}" data-rating="known" aria-pressed="${rating === "known"}" class="${rating === "known" ? "selected" : ""}"><span aria-hidden="true">✓</span> Answers match</button>
        <button type="button" data-rating-key="${escapeHtml(key)}" data-rating="missed" aria-pressed="${rating === "missed"}" class="${rating === "missed" ? "selected" : ""}"><span aria-hidden="true">↻</span> Answers don't match</button>
      </div>
    </div>`;
  }

  // Personal note, hidden behind a toggle; it is only offered once the exam is submitted.
  function noteMarkup(question) {
    if (!("my_note" in question)) return "";
    const note = noteState.get(question.id) || {};
    const label = note.open ? "📝 Hide note ▴" : question.my_note ? "📝 Show note" : "📝 Add note";
    let html = `<div class="cmtwrap exam-note-wrap"><button class="btn${question.my_note ? " has-note" : ""}" type="button" data-note-toggle aria-expanded="${!!note.open}">${label}</button>`;
    if (note.open) {
      html += `<div class="cmtbox question-note">
        <textarea class="question-note-text" rows="4" maxlength="5000" placeholder="Something to remember next time you meet this question…">${escapeHtml(note.text ?? question.my_note)}</textarea>
        <div class="row" style="margin-top:6px"><span class="mute">Only you see this. Questions with notes are kept in your Saved questions.</span>
          <span>${question.my_note ? `<button class="btn" type="button" data-note-delete ${note.busy ? "disabled" : ""}>Delete</button> ` : ""}<button class="btn pri" type="button" data-note-save ${note.busy ? "disabled" : ""}>${note.busy ? "Saving…" : "Save note"}</button></span></div>
        ${note.err ? `<div class="fb bad">${escapeHtml(note.err)}</div>` : ""}${note.msg ? `<div class="fb ok">${escapeHtml(note.msg)}</div>` : ""}</div>`;
    }
    return html + "</div>";
  }

  async function saveNote(question, text) {
    const note = noteState.get(question.id);
    note.busy = true; note.err = ""; note.msg = "";
    renderQuestion();
    try {
      const data = await fetchJson(root.dataset.note.replace("/0/", `/${question.id}/`), {
        method: "POST", body: JSON.stringify({text}),
      });
      question.my_note = data.note;
      note.text = data.note;
      note.msg = data.note ? "✓ Note saved." : "Note deleted.";
    } catch (error) {
      note.err = error.message || "Could not save the note.";
    }
    note.busy = false;
    if (getQuestion() === question) renderQuestion();
  }

  function renderQuestion() {
    const question = getQuestion();
    if (!question) return;
    const review = state.submitted;
    const response = question.response || {};
    const marked = markedQuestions.has(question.id);
    const head = `<div class="qh">
      <div class="quiz-question-context">
      <span class="qn quiz-position"><span>${String(examQuestionNumber(question)).padStart(2, "0")}</span><span class="quiz-position-divider">/</span><span class="quiz-position-total">${String(state.allQuestions.length || state.questions.length).padStart(2, "0")}</span></span>
      <span class="tag">${escapeHtml(question.kind === "desc" ? "Descriptive" : question.kind === "identify" ? "Identify" : question.kind === "mcq" ? "Multiple choice" : "True / False")}</span></div>
      ${!review && !state.assessmentMode ? `<button class="exam-mark-review${marked ? " is-marked" : ""}" type="button" data-mark-review aria-pressed="${marked}" aria-label="${marked ? "Remove review mark" : "Mark question for review"}" title="${marked ? "Remove review mark" : "Mark question for review"}">${marked ? "Marked" : "Mark"}</button>` : ""}</div>`;
    let html = `<div class="stem">${escapeHtml(question.stem)}</div>`;
    // The figure stays in view (left column on wide screens, top on phones) while answers scroll, as in practice.
    let figure = "";
    if (question.image_url) {
      const style = question.image_width > 0 && question.image_height > 0
        ? ` style="--image-aspect-ratio:${question.image_width}/${question.image_height};--image-w-per-h:${question.image_width / question.image_height};--image-max-width:${78 * question.image_width / question.image_height}vh"` : "";
      const markers = (question.targets || []).filter(target =>
        target.x_percent !== null && target.y_percent !== null
      ).map(target => `<span class="image-marker" style="left:${target.x_percent}%;top:${target.y_percent}%">${escapeHtml(target.label)}</span>`).join("");
      figure = `<figure class="quiz-image-figure"><div class="image-stage${style ? " is-calibrated" : ""}"${style}><img src="${escapeHtml(question.image_url)}" alt="Question image"><div class="image-markers" aria-hidden="true">${markers}</div></div><figcaption><a href="${escapeHtml(question.image_url)}" target="_blank" rel="noopener">Open original image</a></figcaption></figure>`;
    }

    if (question.kind === "mcq") {
      html += `<div class="exam-options">${question.options.map(option => optionMarkup(question, option, review)).join("")}</div>`;
    } else if (question.kind === "tf") {
      html += question.options.map(option => {
        const answer = (response.answers || {})[String(option.id)];
        const correct = review && answer === question.answer_key[String(option.id)];
        return `<div class="tf exam-tf ${review ? (correct ? "exam-correct" : "exam-incorrect") : ""}">
          <span class="t"><b>${escapeHtml(option.letter)}.</b> ${escapeHtml(option.text)}</span>
          <span class="tfb">
            ${[true, false].map(value => `<label><input type="radio" name="tf-${option.id}" value="${value}" ${answer === value ? "checked" : ""} ${review ? "disabled" : ""}>${value ? "True" : "False"}</label>`).join("")}
          </span>
        </div>`;
      }).join("");
    } else if (question.kind === "desc") {
      if (review) {
        html += `<div class="exam-assessment-compare">
          <section class="exam-assessment-answer exam-assessment-your-answer">
            <span class="exam-assessment-label">YOUR RESPONSE</span>
            <p>${escapeHtml(response.text || "You left this answer blank.")}</p>
          </section>
          <section class="exam-assessment-answer exam-assessment-suggested-answer">
            <span class="exam-assessment-label">SUGGESTED ANSWER</span>
            <p>${escapeHtml(question.answer || "No answer was provided.")}</p>
            ${question.answer_image_url ? `<img class="exam-image exam-answer-image" src="${escapeHtml(question.answer_image_url)}" alt="Answer image">` : ""}
          </section>
        </div>`;
        html += reviewRating(question, "answer");
      } else {
        html += `<label class="exam-answer-label" for="exam-descriptive-answer">Your answer</label>
          <textarea id="exam-descriptive-answer" rows="5" maxlength="10000">${escapeHtml(response.text || "")}</textarea>`;
      }
    } else if (question.kind === "identify") {
      html += `<div class="exam-targets">${(question.targets || []).map(target => {
        const targetId = String(target.id);
        const input = (response.targets || {})[targetId] || "";
        return `<div class="exam-target">
          <label for="target-${target.id}"><strong>${escapeHtml(target.label)}</strong></label>
          ${review
            ? `<div class="exam-assessment-compare exam-identify-compare">
                <section class="exam-assessment-answer exam-assessment-your-answer">
                  <span class="exam-assessment-label">YOUR RESPONSE</span>
                  <p>${escapeHtml(input || "You left this target blank.")}</p>
                </section>
                <section class="exam-assessment-answer exam-assessment-suggested-answer">
                  <span class="exam-assessment-label">CORRECT ANSWER</span>
                  <p>${escapeHtml(target.answer || "No answer was provided.")}</p>
                </section>
              </div>${reviewRating(question, targetId, target.label)}`
            : `<input id="target-${target.id}" type="text" data-target-id="${target.id}" value="${escapeHtml(input)}" maxlength="1000" autocomplete="off">`}
        </div>`;
      }).join("")}</div>`;
    }

    if (review && question.explanation) html += `<div class="fb ok exam-feedback"><strong>Explanation</strong><p>${escapeHtml(question.explanation)}</p></div>`;
    if (review && question.note) html += `<div class="fb warning exam-feedback exam-note"><strong>Note</strong><p>${escapeHtml(question.note)}</p></div>`;
    html = head + (figure ? `<div class="image-question-layout">${figure}<div class="image-question-side">${html}</div></div>` : html);
    if (review) html += noteMarkup(question);
    questionElement.className = `card quiz-question exam-question${question.kind === "desc" ? " flashcard descriptive-question" : ""}${question.kind === "mcq" ? " quiz-question-mcq" : question.kind === "tf" ? " quiz-question-tf" : ""}${figure ? " quiz-question-has-image" : ""}`;
    questionElement.innerHTML = html;
    position.textContent = state.assessmentMode
      ? `${state.questions.length} question${state.questions.length === 1 ? "" : "s"} still need assessment`
      : state.reportQuestionReview
        ? `Reviewing question ${examQuestionNumber(question)} of ${state.questions.length}`
      : `Question ${state.current + 1} of ${state.questions.length}`;
    if (progressBar) {
      const progress = state.assessmentMode
        ? assessmentProgress().complete / Math.max(assessmentProgress().total, 1)
        : (state.current + 1) / state.questions.length;
      progressBar.style.width = `${progress * 100}%`;
    }
    previous.disabled = state.current === 0;
    next.disabled = state.current === state.questions.length - 1;
    submit.hidden = state.finalized;
    submit.textContent = review ? "View detailed results" : "Submit exam";
    submit.disabled = review && !selfAssessmentComplete();
    navigation.querySelectorAll("button").forEach((button, index) => {
      button.classList.toggle("cur", index === state.current);
    });
  }

  function renderNavigation() {
    navigation.innerHTML = "";
    state.questions.forEach((question, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = state.assessmentMode ? String(examQuestionNumber(question)) : String(index + 1);
      button.setAttribute("aria-label", state.assessmentMode
        ? `Go to assessment question ${examQuestionNumber(question)}`
        : `${markedQuestions.has(question.id) ? "Marked for review, " : ""}Go to question ${index + 1}`);
      button.addEventListener("click", () => {
        state.current = index;
        renderQuestion();
      });
      navigation.appendChild(button);
    });
    renderQuestion();
    if (answeredCount) {
      const count = state.questions.filter(question => {
        const response = question.response || {};
        return response.selected != null
          || Object.keys(response.answers || {}).length > 0
          || Boolean((response.text || "").trim())
          || Object.keys(response.targets || {}).some(key => Boolean(response.targets[key].trim()));
      }).length;
      answeredCount.textContent = `${count} / ${state.questions.length} answered`;
    }
  }

  function recordAnswer(question) {
    status.textContent = "Answer recorded on this page. It will be sent when you submit the exam.";
    renderNavigationState();
  }

  function renderNavigationState() {
    let answeredCountValue = 0;
    state.questions.forEach((question, index) => {
      const button = navigation.children[index];
      const response = question.response || {};
      const answered = response.selected != null
        || Object.keys(response.answers || {}).length > 0
        || Boolean((response.text || "").trim())
        || Object.keys(response.targets || {}).some(key => Boolean(response.targets[key].trim()));
      if (answered) answeredCountValue += 1;
      if (button) {
        button.classList.toggle("answered", answered);
        button.classList.toggle("marked", markedQuestions.has(question.id));
        if (!state.assessmentMode) {
          button.setAttribute("aria-label", `${markedQuestions.has(question.id) ? "Marked for review, " : ""}Go to question ${index + 1}`);
        }
      }
    });
    if (answeredCount) answeredCount.textContent = `${answeredCountValue} / ${state.questions.length} answered`;
  }

  function updateRatingControls(question) {
    questionElement.querySelectorAll("[data-rating-key]").forEach(button => {
      const selected = question.response.ratings?.[button.dataset.ratingKey] === button.dataset.rating;
      button.classList.toggle("selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    submit.disabled = !selfAssessmentComplete();
  }

  questionElement.addEventListener("change", event => {
    if (state.submitted) return;
    const question = getQuestion();
    if (question.kind === "mcq" && event.target.name === "mcq-answer") {
      question.response.selected = Number(event.target.value);
      questionElement.querySelectorAll(".exam-option").forEach(option => {
        option.classList.toggle("exam-selected", option.querySelector("input").checked);
      });
    } else if (question.kind === "tf" && event.target.type === "radio") {
      question.response.answers = question.response.answers || {};
      question.response.answers[event.target.name.slice(3)] = event.target.value === "true";
    } else if (question.kind === "identify" && event.target.dataset.targetId) {
      question.response.targets = question.response.targets || {};
      question.response.targets[event.target.dataset.targetId] = event.target.value;
    } else {
      return;
    }
    recordAnswer(question);
  });
  questionElement.addEventListener("input", event => {
    if (state.submitted) return;
    const question = getQuestion();
    if (question.kind === "desc" && event.target.id === "exam-descriptive-answer") {
      question.response.text = event.target.value;
    } else if (question.kind === "identify" && event.target.dataset.targetId) {
      question.response.targets = question.response.targets || {};
      question.response.targets[event.target.dataset.targetId] = event.target.value;
    } else {
      return;
    }
    recordAnswer(question);
  });
  function persistRatings(question) {
    const timer = ratingTimers.get(question.id);
    if (timer) {
      clearTimeout(timer);
      ratingTimers.delete(question.id);
    }
    const previousSave = ratingSaves.get(question.id) || Promise.resolve();
    const ratings = { ...(question.response.ratings || {}) };
    const pending = previousSave.catch(() => {}).then(() => fetchJson(root.dataset.answer, {
      method: "POST",
      body: JSON.stringify({ question_id: question.id, ratings }),
    }));
    ratingSaves.set(question.id, pending);
    pending.then(() => {
      status.textContent = selfAssessmentComplete()
        ? "Self-assessment saved. You can now view your detailed results."
        : "Self-assessment saved. Rate each remaining descriptive answer and Identify target.";
    }).catch(error => {
      status.textContent = `Unable to save self-assessment: ${error.message}`;
    }).finally(() => {
      if (ratingSaves.get(question.id) === pending) ratingSaves.delete(question.id);
    });
  }

  function scheduleRatingSave(question) {
    const existing = ratingTimers.get(question.id);
    if (existing) clearTimeout(existing);
    ratingTimers.set(question.id, setTimeout(() => persistRatings(question), 120));
  }

  async function flushRatingSaves() {
    ratingTimers.forEach((timer, questionId) => {
      clearTimeout(timer);
      const question = state.allQuestions.find(item => item.id === questionId);
      if (question) persistRatings(question);
    });
    ratingTimers.clear();
    await Promise.all([...ratingSaves.values()]);
  }

  questionElement.addEventListener("input", event => {
    if (!event.target.matches("textarea.question-note-text")) return;
    (noteState.get(getQuestion().id) || {}).text = event.target.value;
  });
  questionElement.addEventListener("click", event => {
    const current = getQuestion();
    if (event.target.closest("[data-note-toggle]")) {
      const note = noteState.get(current.id) || {};
      noteState.set(current.id, {open: !note.open, text: current.my_note || ""});
      renderQuestion();
      if (!note.open) questionElement.querySelector("textarea.question-note-text")?.focus();
      return;
    }
    if (event.target.closest("[data-note-save]")) {
      saveNote(current, noteState.get(current.id).text || "");
      return;
    }
    if (event.target.closest("[data-note-delete]")) {
      if (confirm("Delete your note on this question?")) saveNote(current, "");
      return;
    }
    const markButton = event.target.closest("[data-mark-review]");
    if (markButton && !state.submitted) {
      const question = getQuestion();
      if (markedQuestions.has(question.id)) {
        markedQuestions.delete(question.id);
      } else {
        markedQuestions.add(question.id);
      }
      renderQuestion();
      renderNavigationState();
      return;
    }
    const button = event.target.closest("[data-rating]");
    if (!button || !state.submitted) return;
    const question = getQuestion();
    question.response.ratings = question.response.ratings || {};
    const key = button.dataset.ratingKey;
    if (button.dataset.rating === "known") {
      const response = question.kind === "desc"
        ? question.response.text
        : question.response.targets?.[key];
      if (!response?.trim()) {
        status.textContent = question.kind === "desc"
          ? "Write your answer before marking it as a match."
          : "Enter your response before marking it as a match.";
        return;
      }
    }
    question.response.ratings[key] = button.dataset.rating;
    updateRatingControls(question);
    updateAssessmentProgress();
    scheduleRatingSave(question);
    if (questionAssessmentComplete(question)) {
      state.questions = state.questions.filter(item => item.id !== question.id);
      state.current = Math.min(state.current, state.questions.length - 1);
      if (state.questions.length) {
        renderNavigation();
        renderNavigationState();
        status.textContent = "Assessment saved. Moving to the next question that needs assessment.";
      } else {
        renderAssessmentComplete();
      }
    } else {
      const nextTask = nextAssessmentTask(question, key);
      if (nextTask) {
        const sameQuestion = nextTask.question.id === question.id;
        if (!sameQuestion) {
          state.current = state.questions.findIndex(item => item.id === nextTask.question.id);
          renderNavigation();
          renderNavigationState();
        }
        const nextButton = [...questionElement.querySelectorAll("[data-rating-key]")]
          .find(item => item.dataset.ratingKey === nextTask.key && item.dataset.rating === "known");
        nextButton?.focus();
        nextButton?.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
      status.textContent = "Rating saved. Continue with the next answer that needs assessment.";
    }
  });

  previous.addEventListener("click", () => {
    state.current = Math.max(0, state.current - 1);
    renderQuestion();
  });
  next.addEventListener("click", () => {
    state.current = Math.min(state.questions.length - 1, state.current + 1);
    renderQuestion();
  });
  submit.addEventListener("click", async () => {
    if (state.submitted) {
      await finishSelfAssessment();
      return;
    }
    if (!window.confirm("Submit this exam? You cannot change answers after submitting.")) return;
    finishExam(false);
  });

  function selfAssessmentComplete() {
    return state.allQuestions.every(question =>
      !isAssessmentQuestion(question) || questionAssessmentComplete(question));
  }

  function assessmentProgress() {
    let total = 0;
    let complete = 0;
    state.allQuestions.filter(isAssessmentQuestion).forEach(question => {
      if (question.kind === "desc") {
        total += 1;
        if (["known", "missed"].includes((question.response.ratings || {}).answer)) complete += 1;
      } else {
        question.targets.forEach(target => {
          total += 1;
          if (["known", "missed"].includes((question.response.ratings || {})[String(target.id)])) complete += 1;
        });
      }
    });
    return { complete, total, remaining: total - complete };
  }

  function updateAssessmentProgress() {
    const progress = assessmentProgress();
    const remaining = document.getElementById("exam-assessment-remaining");
    if (remaining) remaining.textContent = String(progress.remaining);
    if (answeredCount) {
      answeredCount.textContent = `${progress.complete} of ${progress.total} assessments complete`;
    }
    if (progressBar) {
      progressBar.style.width = `${progress.complete / Math.max(progress.total, 1) * 100}%`;
    }
  }

  function isAssessmentQuestion(question) {
    return question.kind === "desc" || question.kind === "identify";
  }

  function questionAssessmentComplete(question) {
    const ratings = question.response.ratings || {};
    if (question.kind === "desc") return ["known", "missed"].includes(ratings.answer);
    if (question.kind === "identify") {
      return question.targets.length > 0 && question.targets.every(target =>
        ["known", "missed"].includes(ratings[String(target.id)]));
    }
    return true;
  }

  function nextAssessmentTask(question, ratingKey) {
    const tasks = state.questions.flatMap(item => {
      if (item.kind === "desc") return [{ question: item, key: "answer" }];
      if (item.kind === "identify") {
        return item.targets.map(target => ({ question: item, key: String(target.id) }));
      }
      return [];
    });
    const currentIndex = tasks.findIndex(task => task.question.id === question.id && task.key === ratingKey);
    if (currentIndex < 0) return null;
    for (let offset = 1; offset < tasks.length; offset += 1) {
      const task = tasks[(currentIndex + offset) % tasks.length];
      if (!["known", "missed"].includes((task.question.response.ratings || {})[task.key])) return task;
    }
    return null;
  }

  function renderAssessmentComplete() {
    navigation.hidden = true;
    previous.hidden = true;
    next.hidden = true;
    position.textContent = "Complete";
    progressBar.style.width = "100%";
    questionElement.className = "card exam-assessment-complete";
    questionElement.innerHTML = `<span class="exam-assessment-complete-mark" aria-hidden="true">✓</span><h2>That’s everything</h2><p>You’ve assessed every answer. Your detailed results are ready whenever you are.</p>`;
    submit.hidden = false;
    submit.disabled = false;
    submit.textContent = "View detailed results";
    updateAssessmentProgress();
    status.textContent = "Self-assessment complete. View your detailed results when you're ready.";
  }

  async function finishSelfAssessment() {
    if (!selfAssessmentComplete()) {
      status.textContent = "Rate every descriptive answer and Identify target before viewing your results.";
      return;
    }
    submit.disabled = true;
    try {
      await flushRatingSaves();
      const result = await fetchJson(root.dataset.api);
      if (!result.report_ready) {
        status.textContent = "Some self-assessments are not saved yet. Please try again.";
        submit.disabled = false;
        return;
      }
      showResults(result);
    } catch (error) {
      status.textContent = error.message;
      submit.disabled = false;
    }
  }

  async function finishExam(force) {
    if (state.finishing || state.submitted) return;
    state.finishing = true;
    submit.disabled = true;
    questionElement.querySelectorAll("input, textarea").forEach(input => { input.disabled = true; });
    try {
      const responses = Object.fromEntries(state.allQuestions.map(question => [
        String(question.id),
        question.response || {},
      ]));
      const result = await fetchJson(root.dataset.submit, {
        method: "POST",
        body: JSON.stringify({ responses }),
      });
      if (result.report_ready) showResults(result);
      else showSubmissionReview(result);
    } catch (error) {
      state.finishing = false;
      if (!force && expiresAt && Date.now() >= expiresAt) {
        finishExam(true);
        return;
      }
      status.textContent = error.message;
      submit.disabled = false;
      questionElement.querySelectorAll("input, textarea").forEach(input => { input.disabled = false; });
    }
  }

  function showSubmissionReview(result) {
    assessmentResult = result;
    state.allQuestions = result.questions;
    state.questions = state.allQuestions.filter(question =>
      isAssessmentQuestion(question) && !questionAssessmentComplete(question));
    state.submitted = true;
    state.assessmentMode = true;
    state.reportQuestionReview = false;
    state.finishing = false;
    submit.disabled = false;
    if (countdown) clearInterval(countdown);
    clock.textContent = "Submitted";
    root.classList.add("is-assessing");
    navigation.setAttribute("aria-label", "Assessment question navigation");
    document.getElementById("exam-assessment-heading").hidden = false;
    updateAssessmentProgress();
    state.current = 0;
    if (result.report_ready) {
      showResults(result);
      return;
    }
    if (result.ai_assessment_status === "queued" || result.ai_assessment_status === "processing") {
      showAiAssessmentProgress(result.queue_position);
      pollAiAssessment();
      return;
    }
    if (result.ai_assessment_status === "completed") {
      startManualAssessment(result);
      return;
    }
    if (result.ai_assessment_status === "failed") {
      renderAssessmentChoice(result, result.error);
      return;
    }
    if (result.ai_assessment_status === "cancelled") {
      renderAssessmentChoice(result);
      return;
    }
    renderAssessmentChoice(result);
  }

  function renderAssessmentChoice(result, errorMessage = "") {
    navigation.hidden = true;
    navigation.parentElement.hidden = true;
    previous.hidden = true;
    next.hidden = true;
    position.hidden = true;
    progressBar.parentElement.hidden = true;
    submit.hidden = true;
    document.getElementById("exam-stage-title").textContent = "Choose assessment";
    const hasDescriptive = state.allQuestions.some(question =>
      question.kind === "desc" && !questionAssessmentComplete(question));
    const aiAvailable = hasDescriptive;
    questionElement.className = "card exam-assessment-choice-screen";
    questionElement.innerHTML = `<h2>Choose how to assess your descriptive answers</h2>
      <p>If you choose AI marking, this exam’s descriptive questions, suggested answers, and your written responses will be sent to Google Gemini.</p>
      <div class="exam-assessment-mode-options">
        <button class="btn pri" type="button" data-assessment-ai ${aiAvailable ? "" : "disabled"}>
          Assess descriptive answers with AI
        </button>
        <button class="btn" type="button" data-assessment-manual>Assess manually</button>
      </div>
      <p class="exam-assessment-mode-status" data-assessment-mode-status role="status">${escapeHtml(errorMessage || (aiAvailable ? "" : "This exam has no descriptive answers for AI marking."))}</p>`;
    const aiButton = questionElement.querySelector("[data-assessment-ai]");
    const manualButton = questionElement.querySelector("[data-assessment-manual]");
    const modeStatus = questionElement.querySelector("[data-assessment-mode-status]");
    aiButton.addEventListener("click", async () => {
      try {
        await fetchJson(root.dataset.aiAssess, {
          method: "POST",
          body: "{}",
        });
        showAiAssessmentProgress();
        pollAiAssessment();
      } catch (error) {
        modeStatus.textContent = `${error.message} You can still assess manually.`;
        status.textContent = "AI assessment is unavailable. Manual assessment is still available.";
      }
    });
    manualButton.addEventListener("click", () => {
      cancelQueuedAiAssessment();
      startManualAssessment(result);
    });
    status.textContent = errorMessage || (aiAvailable
      ? "Choose AI or manual assessment to continue."
      : "This exam has no descriptive answers for AI marking.");
  }

  function showAiAssessmentProgress(queuePosition = 0) {
    navigation.hidden = true;
    navigation.parentElement.hidden = true;
    previous.hidden = true;
    next.hidden = true;
    position.hidden = true;
    progressBar.parentElement.hidden = true;
    submit.hidden = true;
    document.getElementById("exam-stage-title").textContent = "AI assessment";
    const queueMessage = queuePosition > 1
      ? `There ${queuePosition - 1 === 1 ? "is" : "are"} ${queuePosition - 1} ${queuePosition - 1 === 1 ? "request" : "requests"} ahead of you.`
      : queuePosition === 1
        ? "Your request is at the front of the queue."
        : "Your request is joining the queue.";
    questionElement.className = "card exam-ai-loading-screen";
    questionElement.innerHTML = `<div class="exam-ai-loader" role="status" aria-label="AI assessment in progress"></div>
      <h2>Preparing your results</h2>
      <p data-ai-queue-message>${queueMessage}</p>
      <p class="exam-ai-loader-note">We’ll show your results here as soon as your answers have been assessed. Keep this page open; you can also switch to manual assessment.</p>
      <button class="btn" type="button" data-ai-manual>Continue with manual assessment</button>`;
    questionElement.querySelector("[data-ai-manual]").addEventListener("click", () => {
      if (aiPollTimer) clearTimeout(aiPollTimer);
      aiPollTimer = null;
      cancelQueuedAiAssessment();
      startManualAssessment(assessmentResult);
    });
    status.textContent = "Your AI assessment is in the queue.";
  }

  function cancelQueuedAiAssessment() {
    fetchJson(root.dataset.aiAssess, {
      method: "POST",
      body: JSON.stringify({ action: "cancel" }),
    }).catch(error => console.warn("Unable to cancel the queued AI assessment.", error));
  }

  function pollAiAssessment() {
    if (aiPollTimer) clearTimeout(aiPollTimer);
    const check = async () => {
      try {
        const result = await fetchJson(root.dataset.aiAssess);
        if (result.assessment_status === "completed") {
          aiPollTimer = null;
          if (result.report_ready) showResults(result);
          else startManualAssessment(result);
          return;
        }
        if (result.assessment_status === "failed") {
          aiPollTimer = null;
          renderAssessmentChoice(assessmentResult, result.error || "AI assessment couldn’t be completed. You can try again or assess manually.");
          return;
        }
        const message = questionElement.querySelector("[data-ai-queue-message]");
        if (message) {
          message.textContent = result.assessment_status === "processing"
            ? "Gemini is assessing your descriptive answers now."
            : result.queue_position > 1
              ? `There are ${result.queue_position - 1} requests ahead of you.`
              : "Your request is at the front of the queue.";
        }
        aiPollTimer = setTimeout(check, 4000);
      } catch (error) {
        const message = questionElement.querySelector("[data-ai-queue-message]");
        if (message) message.textContent = `${error.message} Reconnecting…`;
        aiPollTimer = setTimeout(check, 5000);
      }
    };
    aiPollTimer = setTimeout(check, 250);
  }

  function startManualAssessment(result) {
    state.allQuestions = result.questions;
    state.questions = state.allQuestions.filter(question =>
      isAssessmentQuestion(question) && !questionAssessmentComplete(question));
    state.assessmentMode = true;
    submit.hidden = false;
    submit.disabled = false;
    submit.textContent = "View detailed results";
    document.getElementById("exam-assessment-heading").querySelector("h1").textContent =
      "Assess your answers";
    document.getElementById("exam-assessment-heading").querySelector("p").textContent =
      "Compare each response with the suggested answer, then choose honestly.";
    navigation.hidden = false;
    navigation.parentElement.hidden = false;
    previous.hidden = false;
    next.hidden = false;
    position.hidden = false;
    progressBar.parentElement.hidden = false;
    document.getElementById("exam-stage-title").textContent = "Manual assessment";
    renderNavigation();
    renderNavigationState();
    updateAssessmentProgress();
    if (!state.questions.length) {
      renderAssessmentComplete();
      submit.disabled = true;
      status.textContent = "No manual assessments remain, but the report is not ready. Please contact support.";
      return;
    }
    status.textContent = "Compare each response with the suggested answer, then rate your answer.";
    renderQuestion();
  }

  function showResults(result) {
    state.questions = result.questions;
    state.allQuestions = result.questions;
    state.submitted = true;
    state.assessmentMode = false;
    state.reportQuestionReview = false;
    state.finalized = true;
    state.finishing = false;
    const aiMarkedResults = (result.report || []).some(row => row.reason.startsWith("AI marked"));
    if (aiMarkedResults && document.hidden) {
      document.title = `Results ready · ${examPageTitle}`;
      if (typeof Notification !== "undefined" && Notification.permission === "granted") {
        try {
          new Notification("Your AI assessment is ready", {
            body: "Return to your exam page to view the results.",
          });
        } catch (error) {
          console.warn("Unable to show the AI assessment notification.", error);
        }
      }
    } else {
      document.title = examPageTitle;
    }
    if (countdown) clearInterval(countdown);
    clock.textContent = "Results";
    root.classList.remove("is-assessing");
    navigation.hidden = true;
    navigation.setAttribute("aria-label", "Question navigation");
    document.getElementById("exam-assessment-heading").hidden = true;
    navigation.parentElement.hidden = true;
    progressBar.parentElement.hidden = true;
    position.hidden = true;
    questionElement.className = "card quiz-report";
    const wrong = result.missed || [];
    const reportLink = item => {
      const questionIndex = state.questions.findIndex(question => question.id === item.id);
      const questionNumber = questionIndex >= 0 ? questionIndex + 1 : "";
      return `<li class="exam-report-list-item"><a class="exam-report-link" href="#" data-report-question="${item.id}" aria-label="Review exam question ${questionNumber}">
        <span class="exam-report-mark ${item.earned === item.possible ? "exam-result-correct" : "exam-result-missed"}">${item.earned === item.possible ? "✓" : "✗"} Q${questionNumber}</span>
        <span class="exam-report-copy">${escapeHtml(item.stem.slice(0, 180))}<small>${escapeHtml(item.reason)} · ${item.earned}/${item.possible} points</small></span>
      </a></li>`;
    };
    const reportIndex = new Map(state.questions.map((question, index) => [question.id, index]));
    const numberedMissed = wrong.map(item => ({
      ...item,
      questionNumber: (reportIndex.get(item.id) ?? -1) + 1,
    })).sort((a, b) => a.questionNumber - b.questionNumber);
    questionElement.innerHTML = `<div class="quiz-report-summary">
      <div class="big quiz-score">${result.score_percent}<span>%</span></div>
      <div><h2 class="quiz-report-title">Mock exam results</h2>
        <p><strong>${result.points_earned} / ${result.points_possible}</strong> points earned · ${result.question_count} questions</p>
        <p>MCQ and True / False were scored automatically. Descriptive answers use AI marking or self-assessment; Identify answers use self-assessment.</p>
      </div>
    </div>
    <h3 class="quiz-report-list-title">${wrong.length ? `Questions to review (${wrong.length})` : "No questions to review — excellent work!"}</h3>
    ${wrong.length ? `<ul class="w quiz-report-list">${numberedMissed.map(reportLink).join("")}</ul>` : ""}
    <h3 class="quiz-report-list-title">Question-by-question results</h3>
    <ul class="w quiz-report-list">${result.report.map(reportLink).join("")}</ul>
    <section class="exam-share-controls" aria-label="Share exam">
      <div>
        <h3>Share your exam</h3>
        <p>Invite someone to take the same questions in their own fresh attempt.</p>
      </div>
      <button class="btn pri" type="button" data-share-create>Create share link</button>
      <div class="exam-share-link" data-share-link${result.share_url ? "" : " hidden"}>
        <label for="exam-share-url">Share link</label>
        <input id="exam-share-url" type="url" readonly value="${escapeHtml(result.share_url || "")}">
        <div class="exam-share-actions">
          <button class="btn" type="button" data-share-copy>Copy link</button>
          <a class="btn exam-share-whatsapp" href="#" target="_blank" rel="noopener" data-share-whatsapp>
            <svg viewBox="0 0 32 32" aria-hidden="true" focusable="false">
              <path d="M16 3.25A12.6 12.6 0 0 0 5.18 22.3L3.5 28.5l6.36-1.66A12.6 12.6 0 1 0 16 3.25Z"></path>
              <path d="M12.15 10.1c-.32-.72-.66-.73-.97-.74h-.83c-.29 0-.76.11-1.16.55-.4.44-1.52 1.49-1.52 3.64s1.56 4.23 1.78 4.52c.22.29 3.02 4.84 7.46 6.59 3.69 1.45 4.44 1.16 5.24 1.09.8-.07 2.58-1.05 2.95-2.07.36-1.02.36-1.89.25-2.07-.11-.18-.4-.29-.83-.51-.44-.22-2.58-1.27-2.98-1.42-.4-.14-.69-.22-.98.22-.29.44-1.12 1.42-1.38 1.71-.25.29-.51.33-.94.11-.44-.22-1.84-.68-3.5-2.16-1.29-1.15-2.16-2.58-2.42-3.02-.25-.44-.03-.67.19-.89.2-.2.44-.51.65-.76.22-.25.29-.44.44-.73.14-.29.07-.55-.04-.76-.11-.22-.95-2.38-1.31-3.24Z"></path>
            </svg>
            Share on WhatsApp
          </a>
          <button class="btn" type="button" data-share-revoke>Revoke link</button>
        </div>
      </div>
      <p class="exam-share-status" data-share-status role="status" aria-live="polite"></p>
    </section>
    <section class="exam-save-controls" aria-label="Save exam template">
      <div>
        <h3>Keep this exam</h3>
        <p>Save the exact questions as a reusable template, without your answers.</p>
      </div>
      <button class="btn" type="button" data-save-template${result.template_saved ? " disabled" : ""}>
        ${result.template_saved ? "Saved to My templates" : "Save exam"}
      </button>
      <p class="exam-save-status" data-save-status role="status" aria-live="polite">${result.template_saved ? "Saved without answers. Your completed attempt remains in Recent exam attempts." : ""}</p>
    </section>
    <div class="result-actions">
      <a class="btn pri" href="${escapeHtml(root.dataset.back)}">Back to exams</a>
      ${root.dataset.practiceAgain ? `<form method="post" action="${escapeHtml(root.dataset.practiceAgain)}">
        <input type="hidden" name="csrfmiddlewaretoken" value="${escapeHtml(csrf())}">
        <button class="btn" type="submit">Practice again</button>
      </form>` : ""}
      <form method="post" action="${escapeHtml(root.dataset.deleteAttempt)}" onsubmit="return confirm('Delete this exam attempt and its answers? This cannot be undone.')">
        <input type="hidden" name="csrfmiddlewaretoken" value="${escapeHtml(csrf())}">
        <button class="btn" type="submit">Delete attempt</button>
      </form>
    </div>`;
    submit.hidden = true;
    previous.hidden = true;
    next.hidden = true;
    position.textContent = "Complete";
    status.textContent = `Final score: ${result.score_percent} out of 100.`;
    const shareLink = questionElement.querySelector("[data-share-link]");
    const shareInput = questionElement.querySelector("#exam-share-url");
    const shareStatus = questionElement.querySelector("[data-share-status]");
    const whatsappLink = questionElement.querySelector("[data-share-whatsapp]");
    const saveButton = questionElement.querySelector("[data-save-template]");
    const saveStatus = questionElement.querySelector("[data-save-status]");
    const setShareLink = url => {
      shareInput.value = url || "";
      shareLink.hidden = !url;
      questionElement.querySelector("[data-share-create]").hidden = Boolean(url);
      whatsappLink.href = url
        ? `https://wa.me/?text=${encodeURIComponent(`Practice this exam: ${url}`)}`
        : "#";
    };
    setShareLink(result.share_url);
    questionElement.querySelector("[data-share-create]").addEventListener("click", async event => {
      const button = event.currentTarget;
      button.disabled = true;
      shareStatus.textContent = "Creating your link…";
      try {
        const shared = await fetchJson(root.dataset.share, {
          method: "POST",
          body: JSON.stringify({ action: "create" }),
        });
        setShareLink(shared.share_url);
        shareStatus.textContent = "Your link is ready to share.";
      } catch (error) {
        shareStatus.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
    saveButton.addEventListener("click", async () => {
      saveButton.disabled = true;
      saveStatus.textContent = "Saving this question set…";
      try {
        await fetchJson(root.dataset.saveTemplate, {
          method: "POST",
          body: "{}",
        });
        saveButton.textContent = "Saved to My templates";
        saveStatus.textContent = "Saved without answers. Your completed attempt remains in Recent exam attempts.";
      } catch (error) {
        saveStatus.textContent = error.message;
        saveButton.disabled = false;
      }
    });
    questionElement.querySelector("[data-share-copy]").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(shareInput.value);
        shareStatus.textContent = "Link copied.";
      } catch (error) {
        console.warn("Unable to copy the exam share link.", error);
        shareInput.focus();
        shareInput.select();
        shareStatus.textContent = "Copy is unavailable here. Select the link above and copy it.";
      }
    });
    questionElement.querySelector("[data-share-revoke]").addEventListener("click", async event => {
      const button = event.currentTarget;
      button.disabled = true;
      shareStatus.textContent = "Revoking the link…";
      try {
        await fetchJson(root.dataset.share, {
          method: "POST",
          body: JSON.stringify({ action: "revoke" }),
        });
        setShareLink(null);
        shareStatus.textContent = "The share link has been revoked.";
      } catch (error) {
        shareStatus.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
    questionElement.querySelectorAll("[data-report-question]").forEach(link => {
      link.addEventListener("click", event => {
        event.preventDefault();
        const reportQuestionId = Number(link.dataset.reportQuestion);
        const questionIndex = state.questions.findIndex(question =>
          question.id === reportQuestionId);
        if (questionIndex < 0) {
          status.textContent = "That question is no longer available in this exam.";
          return;
        }
        state.finalized = false;
        state.reportQuestionReview = true;
        state.current = questionIndex;
        navigation.hidden = false;
        navigation.parentElement.hidden = false;
        progressBar.parentElement.hidden = false;
        position.hidden = false;
        questionElement.className = "card quiz-question exam-question";
        previous.hidden = false;
        next.hidden = false;
        submit.hidden = false;
        submit.textContent = "View detailed results";
        renderNavigation();
        renderNavigationState();
      });
    });
  }

  function startClock(expiresAtValue) {
    if (!expiresAtValue) {
      clock.textContent = "Untimed";
      return;
    }
    expiresAt = new Date(expiresAtValue).getTime();
    const expiry = expiresAt;
    const tick = () => {
      const remaining = Math.max(0, expiry - Date.now());
      const minutes = Math.floor(remaining / 60000);
      const seconds = Math.floor((remaining % 60000) / 1000);
      clock.textContent = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")} remaining`;
      if (remaining === 0) {
        clearInterval(countdown);
        status.textContent = "Time is up. Submitting your exam…";
        finishExam(true);
      }
    };
    tick();
    countdown = setInterval(tick, 1000);
  }

  fetchJson(root.dataset.api)
    .then(result => {
      if (!result.questions.length) throw new Error("This exam has no available questions.");
      state.questions = result.questions;
      state.allQuestions = result.questions;
      state.submitted = result.submitted;
      renderNavigation();
      renderNavigationState();
      if (result.submitted && result.report_ready) showResults(result);
      else if (result.submitted) showSubmissionReview(result);
      else {
        status.textContent = "Answers stay on this page and are sent when you submit. Refreshing may lose them.";
        startClock(result.expires_at);
      }
    })
    .catch(error => { status.textContent = error.message; });
})();
