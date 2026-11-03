(() => {
  const widget = document.getElementById("resume-activity");
  if (!widget || document.getElementById("quiz") || document.getElementById("exam-runner")) return;

  const returnLink = document.getElementById("resume-activity-return");
  const exitButton = document.getElementById("resume-activity-exit");
  const title = document.getElementById("resume-activity-title");
  const subtitle = document.getElementById("resume-activity-subtitle");
  const activeKey = `gd:activeQuiz:${widget.dataset.userId}`;
  try { localStorage.removeItem("gd:activeQuiz"); } catch (error) {}  // old reminder shared by every account
  const dismissedKey = "gd:dismissedExams";
  let activity = null;
  let activityType = null;

  const readDismissed = () => {
    try {
      const value = JSON.parse(sessionStorage.getItem(dismissedKey) || "[]");
      return Array.isArray(value) ? value.map(String) : [];
    } catch (error) {
      console.warn("Unable to read dismissed exam reminders.", error);
      return [];
    }
  };

  const render = () => {
    let activeQuiz = null;
    try {
      activeQuiz = JSON.parse(localStorage.getItem(activeKey) || "null");
    } catch (error) {
      console.warn("Unable to read the active quiz reminder.", error);
    }
    const exam = document.getElementById("active-exam-resume-context");
    const candidates = [];
    if (activeQuiz && typeof activeQuiz.url === "string" && typeof activeQuiz.key === "string") {
      candidates.push({ ...activeQuiz, id: activeQuiz.key, type: "quiz", updatedAt: Number(activeQuiz.updatedAt) || 0 });
    }
    if (exam) {
      candidates.push({
        id: exam.dataset.id,
        title: exam.dataset.title,
        url: exam.dataset.url,
        updatedAt: Date.parse(exam.dataset.updatedAt) || 0,
        type: "exam",
      });
    }
    candidates.sort((a, b) => b.updatedAt - a.updatedAt);
    activity = candidates.find(item =>
      item.type !== "exam" || !readDismissed().includes(String(item.id))
    ) || null;
    activityType = activity?.type || null;
    if (!activity) {
      widget.hidden = true;
      return;
    }
    title.textContent = activity.title || (activityType === "exam" ? "Mock exam" : "Quiz");
    subtitle.textContent = activityType === "exam" ? "An exam is still in progress." : "Your quiz progress is saved.";
    returnLink.href = activity.url;
    returnLink.textContent = activityType === "exam" ? "Go back to exam" : "Go back to quiz";
    exitButton.textContent = activityType === "exam" ? "Exit exam" : "Exit quiz";
    widget.hidden = false;
  };

  returnLink.addEventListener("click", () => {
    if (activityType === "exam" && activity) {
      const dismissed = readDismissed().filter(id => id !== String(activity.id));
      try {
        sessionStorage.setItem(dismissedKey, JSON.stringify(dismissed));
      } catch (error) {
        console.warn("Unable to clear the dismissed exam reminder.", error);
      }
    }
  });

  exitButton.addEventListener("click", () => {
    if (!activity) return;
    if (activityType === "quiz") {
      try {
        const current = JSON.parse(localStorage.getItem(activeKey) || "null");
        if (current?.key === activity.id) localStorage.removeItem(activeKey);
      } catch (error) {
        console.warn("Unable to dismiss the active quiz reminder.", error);
      }
    } else {
      const dismissed = new Set(readDismissed());
      dismissed.add(String(activity.id));
      try {
        sessionStorage.setItem(dismissedKey, JSON.stringify([...dismissed]));
      } catch (error) {
        console.warn("Unable to dismiss the active exam reminder.", error);
      }
    }
    widget.hidden = true;
  });

  window.addEventListener("gd:resume-activity-change", render);
  window.addEventListener("storage", event => {
    if (event.key === activeKey) render();
  });
  render();
})();
