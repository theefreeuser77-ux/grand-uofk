// Mock exam builder: pick chapters by sub-subject, share the exam between them, and check the
// settings live, so the student sees what is wrong before pressing Start.
(() => {
  const form = document.querySelector("[data-exam-builder]");
  if (!form) return;
  const FORMATS = ["mcq", "tf", "descriptive", "identify"];
  const NAMES = { mcq: "multiple choice", tf: "true/false", descriptive: "descriptive", identify: "identify" };
  const SHORT = { mcq: "MCQ", tf: "T/F", descriptive: "Descriptive", identify: "Identify" };
  const rows = [...form.querySelectorAll("[data-chapter]")];
  const groups = [...form.querySelectorAll("[data-group]")];
  const tick = row => row.querySelector('input[type="checkbox"]');
  const share = row => row.querySelector('input[type="number"]');
  const field = name => form.querySelector(`[name="${name}"]`);
  const mixInput = format => field(`${format}_percent`);
  const bar = {
    main: form.querySelector("[data-bar-main]"),
    sub: form.querySelector("[data-bar-sub]"),
    start: form.querySelector("[data-start]"),
  };
  // Question counts per question source ("all", "uofk", "other"…), per chapter or for the deck.
  const fixedAvailable = form.dataset.available ? JSON.parse(form.dataset.available) : null;
  const rowCounts = new Map(rows.map(row => [row, JSON.parse(row.dataset.counts || "{}")]));
  const source = () => (form.querySelector('[name="source"]:checked') || form.querySelector('[name="source"]') || {}).value || "all";
  const forSource = counts => counts[source()] || counts.all || {};
  const countsOf = row => forSource(rowCounts.get(row));
  const totalOf = row => FORMATS.reduce((sum, format) => sum + num(countsOf(row)[format]), 0);
  const num = value => Number(value) || 0;
  const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;
  const mode = () => (form.querySelector('[name="share_mode"]:checked') || {}).value || "custom";
  const semester = () => (form.querySelector('[name="semester"]:checked') || {}).value || "";

  // Whole numbers in proportion to `weights` that add up to `total` (largest remainder first),
  // the same way the server shares an exam out.
  function allocate(total, weights, minimum = 0) {
    const count = weights.length;
    const sum = weights.reduce((a, b) => a + b, 0);
    const free = total - minimum * count;
    const raw = weights.map(w => minimum + free * (sum ? w / sum : 1 / count));
    const out = raw.map(Math.floor);
    const left = total - out.reduce((a, b) => a + b, 0);
    raw.map((value, index) => [value - out[index], index])
      .sort((a, b) => b[0] - a[0])
      .slice(0, left)
      .forEach(([, index]) => { out[index] += 1; });
    return out;
  }

  function update() {
    const picked = rows.filter(row => tick(row).checked);
    if (mode() !== "custom" && picked.length && picked.length <= 100) {
      const shares = allocate(100, picked.map(row => (mode() === "size" ? totalOf(row) : 1)), 1);
      picked.forEach((row, index) => { share(row).value = shares[index]; });
    }
    rows.forEach(row => {
      const on = tick(row).checked;
      const input = share(row);
      input.disabled = !on;
      input.required = on;
      input.readOnly = mode() !== "custom";
      row.classList.toggle("is-picked", on);
      const total = totalOf(row);
      row.querySelector("[data-chapter-total]").textContent = plural(total, "question");
      row.classList.toggle("is-empty", total === 0);
    });
    groups.forEach(group => {
      const mine = rows.filter(row => group.contains(row));
      const shown = mine.filter(row => !row.hidden);
      const shownPicked = shown.filter(row => tick(row).checked).length;
      const groupPicked = mine.filter(row => tick(row).checked);
      const box = group.querySelector("[data-group-check]");
      box.checked = shown.length > 0 && shownPicked === shown.length;
      box.indeterminate = shownPicked > 0 && shownPicked < shown.length;
      group.hidden = !shown.length;
      group.classList.toggle("has-picked", groupPicked.length > 0);
      const groupShare = groupPicked.reduce((sum, row) => sum + num(share(row).value), 0);
      const questions = shown.reduce((sum, row) => sum + totalOf(row), 0);
      group.querySelector("[data-group-status]").textContent = groupPicked.length
        ? `${groupPicked.length} of ${plural(mine.length, "chapter")} picked · ${groupShare}% of the exam`
        : `${plural(shown.length, "chapter")} · ${plural(questions, "question")}`;
    });

    const available = fixedAvailable ? forSource(fixedAvailable) : Object.fromEntries(FORMATS.map(format =>
      [format, picked.reduce((sum, row) => sum + num(countsOf(row)[format]), 0)]));
    const count = num(field("question_count").value);
    const mix = FORMATS.map(format => num(mixInput(format).value));
    const mixTotal = mix.reduce((a, b) => a + b, 0);
    const need = mixTotal === 100 ? allocate(count, mix) : mix.map(() => 0);
    const chosen = !rows.length || picked.length > 0;
    const problems = [];
    FORMATS.forEach((format, index) => {
      const tile = form.querySelector(`[data-format="${format}"]`);
      const short = chosen && need[index] > num(available[format]);
      tile.classList.toggle("is-short", short);
      tile.classList.toggle("is-off", !mix[index]);
      const have = num(available[format]);
      tile.querySelector("[data-have]").textContent = !chosen ? ""
        : short ? `Only ${have} available` : `${have} available`;
      if (short) problems.push(`not enough ${NAMES[format]} questions (${need[index]} needed)`);
    });
    const total = form.querySelector("[data-mix-total]");
    total.textContent = mixTotal === 100 ? "100% ✓" : `${mixTotal}% (must be 100%)`;
    total.classList.toggle("is-valid", mixTotal === 100);
    if (mixTotal !== 100) problems.unshift(`question types add up to ${mixTotal}%, not 100%`);

    if (rows.length) {
      const shareTotal = picked.reduce((sum, row) => sum + num(share(row).value), 0);
      if (picked.length > 100) problems.unshift("choose at most 100 chapters");
      else if (picked.length && shareTotal !== 100) problems.unshift(`chapter shares add up to ${shareTotal}%, not 100%`);
    }
    if (!count) problems.unshift("enter the number of questions");

    const minutes = num(field("time_limit_minutes").value);
    const summary = FORMATS.filter((_, index) => mix[index]).map(format => SHORT[format]).join(" + ");
    if (rows.length && !picked.length) {
      bar.main.textContent = "No chapters picked yet";
      bar.sub.textContent = "Tick a sub-subject or chapters above.";
    } else {
      bar.main.textContent = (rows.length ? `${plural(picked.length, "chapter")} · ` : "") + plural(count, "question");
      bar.sub.textContent = problems.length
        ? problems[0][0].toUpperCase() + problems[0].slice(1) + "."
        : `${minutes ? `${minutes} min` : "No time limit"} · ${summary}`;
    }
    bar.sub.classList.toggle("is-warn", chosen && problems.length > 0);
    bar.start.disabled = !chosen || problems.length > 0;
  }

  function filterSemester() {
    const value = semester();
    rows.forEach(row => { row.hidden = Boolean(value) && row.dataset.semester !== value; });
    form.querySelectorAll("[data-sem-label]").forEach(label => {
      label.hidden = Boolean(value) && label.dataset.semLabel !== value;
    });
  }

  function setOpen(group, open) {
    group.querySelector("[data-group-body]").hidden = !open;
    group.querySelector("[data-group-toggle]").setAttribute("aria-expanded", String(open));
    group.classList.toggle("is-open", open);
  }

  rows.forEach(row => {
    tick(row).addEventListener("change", update);
    share(row).addEventListener("input", update);
  });
  groups.forEach(group => {
    group.querySelector("[data-group-check]").addEventListener("change", event => {
      rows.filter(row => group.contains(row) && !row.hidden).forEach(row => { tick(row).checked = event.target.checked; });
      update();
    });
    group.querySelector("[data-group-toggle]").addEventListener("click", () => {
      setOpen(group, group.querySelector("[data-group-body]").hidden);
    });
    // Open the groups the student is part-way through, the only group, and any with an error.
    const mine = rows.filter(row => group.contains(row));
    const some = mine.filter(row => tick(row).checked).length;
    setOpen(group, groups.length === 1 || (some > 0 && some < mine.length) || Boolean(group.querySelector(".exam-error")));
  });
  form.querySelectorAll('[name="semester"]').forEach(input => input.addEventListener("change", () => {
    filterSemester();
    update();
  }));
  form.querySelectorAll('[name="share_mode"], [name="source"]').forEach(input => input.addEventListener("change", update));
  const clear = form.querySelector("[data-clear]");
  if (clear) clear.addEventListener("click", () => {
    rows.forEach(row => { tick(row).checked = false; });
    update();
  });
  form.querySelectorAll("[data-preset]").forEach(button => button.addEventListener("click", () => {
    button.dataset.preset.split(",").forEach((value, index) => { mixInput(FORMATS[index]).value = value; });
    update();
  }));
  ["question_count", "time_limit_minutes", ...FORMATS.map(format => `${format}_percent`)]
    .forEach(name => field(name) && field(name).addEventListener("input", update));
  // A share typed by hand switches to "Set % myself".
  rows.forEach(row => share(row).addEventListener("focus", () => {
    const custom = form.querySelector('[name="share_mode"][value="custom"]');
    if (custom && !custom.checked) { custom.checked = true; update(); }
  }));
  form.addEventListener("submit", () => {
    bar.start.disabled = true;
    bar.start.textContent = bar.start.dataset.busy || "Please wait…";
  });
  window.addEventListener("pageshow", () => update());

  filterSemester();
  update();
})();
