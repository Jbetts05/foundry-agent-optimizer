(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  let catalog = null;
  let busy = false;
  let optimizationId = null;
  let selectedCandidate = null;
  let optimizationRunning = false;
  let storageKey = "";

  function saveSession() {
    sessionStorage.setItem(storageKey, JSON.stringify({
      optimizationId, v2: catalog.v2 || null,
    }));
  }

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  async function api(path, body) {
    const response = await fetch(path, body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      let message = `Request failed (${response.status}).`;
      try {
        const error = await response.json();
        if (typeof error.detail === "string") message = error.detail;
      } catch { /* Preserve the HTTP status if the server did not return JSON. */ }
      throw new Error(message);
    }
    return response.json();
  }

  function status(id, text, error = false) {
    $(id).textContent = text;
    $(id).classList.toggle("error", error);
  }

  function controls() {
    const ready = catalog?.configured;
    $("run").disabled = !ready || busy || !$("request").value.trim();
    $("evaluate").disabled = !ready || busy;
    $("case").disabled = !catalog || busy;
    $("request").disabled = !catalog || busy;
    $("optimize").disabled = !ready || optimizationRunning || busy;
    $("apply").disabled = busy || optimizationRunning || !selectedCandidate;
  }

  function metric(container, label, value) {
    const row = node("div", undefined, "metric");
    row.append(node("dt", label), node("dd", value ?? "—"));
    container.append(row);
  }

  function clear(label, waiting) {
    $(label + "-answer").textContent = waiting;
    $(label + "-answer").className = "answer waiting";
    $(label + "-reason").textContent = "";
    $(label + "-checks").replaceChildren();
    $(label + "-metrics").replaceChildren();
    $(label + "-inspect").hidden = true;
    for (const title of ["Response time", "Input tokens", "Output tokens"]) {
      metric($(label + "-metrics"), title, "—");
    }
  }

  function show(label, response) {
    if (!response || response.status === "running") return;
    if (response.status === "failed") {
      $(label + "-answer").className = "answer error";
      $(label + "-answer").textContent = "Request failed";
      $(label + "-reason").textContent = response.error;
      return;
    }
    $(label + "-version").textContent = "Version " + response.version;
    $(label + "-answer").className = "answer";
    $(label + "-answer").textContent = response.result.decision;
    $(label + "-reason").textContent = response.result.reason;
    $(label + "-checks").replaceChildren(...(response.checks || []).map(check =>
      node("span", check.label, "check " + (check.passed ? "pass" : "fail"))
    ));
    const metrics = $(label + "-metrics");
    metrics.replaceChildren();
    metric(metrics, "Response time", (response.latency_ms / 1000).toFixed(2) + " s");
    metric(metrics, "Input tokens", response.usage?.input_tokens?.toLocaleString());
    metric(metrics, "Output tokens", response.usage?.output_tokens?.toLocaleString());
    const fields = [
      ["Discount cap", "discount_cap_percent", "%"], ["Margin floor", "minimum_margin_percent", "%"],
      ["Offer price", "offer_price", ""], ["Projected margin", "projected_margin_percent", "%"],
    ];
    for (const [title, field, unit] of fields) {
      const value = response.result[field];
      if (value !== null) metric(metrics, title, value.toFixed(2) + unit);
    }
    const details = $(label + "-detail");
    details.replaceChildren();
    for (const [title, data] of [
      ["Instructions", response.trace.instructions], ["Business policy", response.trace.policy],
      ["Schema", response.trace.schema], ["Tool calls", response.trace.tool_calls],
      ["Output", response.result], ["Response ID", response.response_id],
    ]) {
      const pre = node("pre", typeof data === "string" ? data : JSON.stringify(data, null, 2));
      pre.tabIndex = 0;
      details.append(node("h4", title), pre);
    }
    $(label + "-inspect").hidden = false;
  }

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

  async function comparison(caseId, text, display = true) {
    const versions = [["v1", catalog.v1]];
    if (catalog.v2) versions.push(["v2", catalog.v2.version]);
    const record = { status: "completed" };
    await Promise.all(versions.map(async ([label, version]) => {
      try {
        record[label] = await api("/api/run", { case_id: caseId, text, version });
      } catch (error) {
        record[label] = { status: "failed", error: error.message };
      }
      if (display) show(label, record[label]);
    }));
    return record;
  }

  $("request-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    clear("v1", "Reviewing the request…");
    clear("v2", catalog.v2 ? "Reviewing the request…" : "Optimize to create V2");
    status("request-status", "Reviewing…");
    controls();
    try {
      const result = await comparison($("case").value, $("request").value);
      const failed = [result.v1, result.v2].filter(Boolean).some(item => item.status === "failed");
      status("request-status", failed ? "A request failed. See the response card." : "Comparison complete.", failed);
    } catch (error) {
      status("request-status", error.message, true);
    } finally {
      busy = false; controls();
    }
  });

  $("case").addEventListener("change", () => {
    const selected = catalog.cases.find(item => item.id === $("case").value);
    $("request").value = selected.prompt;
    clear("v1", "Ready to review");
    clear("v2", catalog.v2 ? "Ready to review" : "Optimize to create V2");
    status("request-status", "");
    controls();
  });
  $("request").addEventListener("input", () => {
    clear("v1", "Ready to review");
    clear("v2", catalog.v2 ? "Ready to review" : "Optimize to create V2");
    controls();
  });

  function renderOptimization(record) {
    optimizationRunning = ["submitting", "queued", "in_progress"].includes(record.status);
    const candidates = record.candidates || [];
    const deployable = candidates.filter(candidate =>
      candidate.id !== record.baseline_id && candidate.instructions && candidate.status === "completed"
    );
    const noCandidate = record.status === "succeeded" && !deployable.length;
    $("optimizer-progress").hidden = !optimizationRunning;
    const descriptions = {
      submitting: "Submitting the optimization job…",
      queued: "Queued. Foundry will evaluate V1 and generate candidates.",
      in_progress: "Evaluating the baseline and candidate instructions…",
      succeeded: catalog.candidate_application_enabled === false
        ? "Optimization complete. V2 is deployed with the reviewed policy correction."
        : "Optimization complete. Review a candidate before creating V2.",
      failed: record.error || "Optimization failed.",
      cancelled: "Optimization was cancelled.",
    };
    const candidateError = candidates.find(candidate => candidate.error)?.error;
    const explanation = (record.warnings || []).join(" ") || candidateError;
    const description = noCandidate
      ? "Optimization finished without an eligible V2 candidate." + (explanation ? " " + explanation : "")
      : descriptions[record.status] || record.status;
    status("optimizer-status", description, record.status === "failed" || noCandidate);
    $("job-details").hidden = false;
    $("job-output").textContent = JSON.stringify({
      job_id: record.job_id, status: record.status,
      baseline_score: record.baseline_score, best_score: record.best_score,
      warnings: record.warnings, error: record.error,
      candidates: candidates.map(({ id, name, status, score, error }) => ({ id, name, status, score, error })),
    }, null, 2);
    $("candidates").replaceChildren();
    for (const candidate of candidates) {
      const button = node("button", candidate.name + " · " +
        (candidate.score == null ? candidate.status : (candidate.score * 100).toFixed(1) + "%"),
      "candidate" + (selectedCandidate?.id === candidate.id ? " selected" : ""));
      button.type = "button";
      button.disabled = !deployable.includes(candidate);
      if (candidate.error) button.title = candidate.error;
      button.addEventListener("click", () => {
        selectedCandidate = candidate;
        $("changes").hidden = false;
        $("changes").open = true;
        $("before").textContent = record.baseline_instructions;
        $("after").textContent = candidate.instructions;
        $("apply-actions").hidden = catalog.candidate_application_enabled === false;
        renderOptimization(record);
      });
      $("candidates").append(button);
    }
    controls();
  }

  async function watchOptimization(identifier) {
    optimizationId = identifier;
    saveSession();
    try {
      while (true) {
        const record = await api("/api/optimizations/" + identifier);
        renderOptimization(record);
        if (!optimizationRunning) break;
        await sleep(6000);
      }
    } catch (error) {
      optimizationRunning = false;
      $("optimizer-progress").hidden = true;
      status("optimizer-status", error.message, true);
      controls();
    }
  }

  $("optimize").addEventListener("click", async () => {
    optimizationRunning = true;
    selectedCandidate = null;
    $("changes").hidden = true;
    $("apply-actions").hidden = true;
    status("optimizer-status", "Submitting the optimization job…");
    $("optimizer-progress").hidden = false;
    controls();
    try {
      const pendingKey = storageKey + ":pending-operation";
      let operationId = sessionStorage.getItem(pendingKey);
      if (!operationId) {
        operationId = crypto.randomUUID();
        sessionStorage.setItem(pendingKey, operationId);
      }
      const record = await api("/api/optimizations", { operation_id: operationId });
      sessionStorage.removeItem(pendingKey);
      renderOptimization(record);
      await watchOptimization(record.id);
    } catch (error) {
      optimizationRunning = false;
      $("optimizer-progress").hidden = true;
      status("optimizer-status", error.message, true);
      controls();
    }
  });

  $("apply").addEventListener("click", async () => {
    if (!selectedCandidate || !optimizationId) return;
    $("apply").disabled = true;
    status("apply-status", "Creating the optimized hosted-agent version…");
    try {
      const deployed = await api("/api/optimizations/" + optimizationId + "/apply", {
        candidate_id: selectedCandidate.id,
      });
      catalog.v2 = deployed;
      saveSession();
      $("v2-version").textContent = "Version " + deployed.version;
      clear("v2", "Ready to review");
      status("apply-status", "V2 is ready. Run the same request or measure all cases.");
    } catch (error) {
      status("apply-status", error.message, true);
    } finally {
      controls();
    }
  });

  $("evaluate").addEventListener("click", async () => {
    if (busy) return;
    busy = true; controls();
    $("evaluation").hidden = false;
    $("evaluation-rows").replaceChildren();
    $("evaluation-totals").replaceChildren();
    const totals = { v1: [0, 0], v2: [0, 0] };
    const failures = { v1: 0, v2: 0 };
    const fullyCorrect = {};
    try {
      for (let index = 0; index < catalog.cases.length; index++) {
        const sample = catalog.cases[index];
        const split = sample.split === "development" ? "Development" : "Holdout";
        fullyCorrect[split] ||= { count: 0, v1: 0, v2: 0 };
        fullyCorrect[split].count++;
        status("evaluation-status", `${index + 1} / ${catalog.cases.length}`);
        const row = node("tr");
        row.append(node("td", sample.title), node("td", sample.expected));
        const result = await comparison(sample.id, sample.prompt, false);
        for (const label of ["v1", "v2"]) {
          const value = result[label];
          const checks = value?.checks || [];
          const passed = checks.filter(check => check.passed).length;
          row.append(node("td", value?.result?.decision || (value?.error ? "Failed" : "—")));
          row.append(node("td", checks.length ? `${passed} / ${checks.length}` : "—"));
          totals[label][0] += passed;
          totals[label][1] += checks.length;
          if (value?.status === "completed" && checks.length && passed === checks.length) {
            fullyCorrect[split][label]++;
          }
          if (value?.status === "failed") {
            failures[label]++;
            row.title = value.error;
          }
        }
        $("evaluation-rows").append(row);
      }
      const summary = node("tr");
      summary.append(node("td", "Checks on completed requests"), node("td", ""));
      for (const label of ["v1", "v2"]) {
        const [passed, count] = totals[label];
        const checked = count ? `${passed} / ${count}` : "—";
        summary.append(node("td", ""), node("td",
          failures[label] ? `${checked}; ${failures[label]} failed` : checked));
      }
      $("evaluation-totals").append(summary);
      for (const [split, values] of Object.entries(fullyCorrect)) {
        const summary = node("tr");
        summary.append(node("td", split + " · all checks passed"), node("td", ""));
        for (const label of ["v1", "v2"]) {
          const measured = label === "v1" || catalog.v2;
          const rate = (100 * values[label] / values.count).toFixed(1);
          summary.append(node("td", ""), node("td",
            measured ? `${values[label]} / ${values.count} (${rate}%)` : "—"));
        }
        $("evaluation-totals").append(summary);
      }
      const failed = failures.v1 + failures.v2;
      status("evaluation-status", failed ? `Incomplete: ${failed} requests failed` : "Complete", failed > 0);
    } catch (error) {
      status("evaluation-status", error.message, true);
    } finally {
      busy = false; controls();
    }
  });

  async function init() {
    clear("v1", "Ready to review");
    clear("v2", "Optimize to create V2");
    try {
      catalog = await api("/api/catalog");
      storageKey = `optimizer-demo:${catalog.agent_name}:${catalog.v1}`;
      const saved = JSON.parse(sessionStorage.getItem(storageKey) || "{}");
      catalog.v2 = catalog.v2 || saved.v2 || null;
      if (catalog.provenance) {
        $("provenance").hidden = false;
        $("provenance").textContent = catalog.provenance;
        $("v2-title").textContent = "Agent V2 · optimized + reviewed";
        $("source-details").hidden = false;
        $("source-output").textContent = JSON.stringify(catalog.version_provenance, null, 2);
      }
      $("case").replaceChildren(...catalog.cases.map(item => {
        const prefix = catalog.provenance
          ? (item.split === "development" ? "Development · " : "Holdout · ") : "";
        const option = node("option", prefix + item.title); option.value = item.id; return option;
      }));
      const initialCase = catalog.cases.find(item => item.id === "challenge-dev-below-floor")
        || catalog.cases[0];
      $("case").value = initialCase.id;
      $("request").value = initialCase.prompt;
      $("model-name").textContent = catalog.model;
      if (catalog.v1) $("v1-version").textContent = "Version " + catalog.v1;
      if (catalog.v2) {
        await api("/api/versions/" + encodeURIComponent(catalog.v2.version));
        $("v2-version").textContent = "Version " + catalog.v2.version;
        clear("v2", "Ready to review");
      }
      if (!catalog.configured) status("request-status", "The Foundry agent has not been configured.", true);
      controls();
      if (catalog.configured && saved.optimizationId
          && saved.optimizationId !== catalog.source_optimization_job) {
        await watchOptimization(saved.optimizationId);
      }
    } catch (error) {
      status("request-status", error.message, true);
    }
  }
  init();
})();
