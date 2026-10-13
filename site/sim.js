/* The pipeline, as a working model. This is a teaching model of ion's real
   rules, with made-up repos and a short stand-in hash:
   - the machine may read and propose freely, but the approve stage is a gate;
   - an approval counts only for the plan hash on display when it was given;
   - change the plan and the hash changes, the approval stops counting;
   - a run touching more than a quarter of the org trips the circuit breaker
     before it writes anything. */
(function () {
  "use strict";
  var root = document.getElementById("sim");
  if (!root) return;
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var $ = function (id) { return root.querySelector("#" + id); };
  var stages = Array.prototype.slice.call(root.querySelectorAll(".stage"));
  var logEl = $("sim-log"), verdict = $("sim-verdict");
  var hashEl = $("sim-hash"), apprEl = $("sim-approved"), planEl = $("sim-plan");
  var blastInput = $("sim-blast"), blastVal = $("sim-blastval"), meter = $("sim-meter"), meterFill = $("sim-meterfill");

  var ORG_REPOS = 80;
  var plans = [
    "[P-0007] merge widget-docs into widget (git subtree add, no squash), then a pointer PR in widget-docs",
    "[P-0007] merge widget-docs into widget (git subtree add, no squash), then a pointer PR in widget-docs, then archive widget-docs",
    "[P-0007] merge widget-docs into widget under docs/ (git subtree add, no squash), then a pointer PR in widget-docs"
  ];
  var S = null;

  function fnv(str) {
    var h = 0x811c9dc5;
    for (var i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
    var out = "";
    for (var k = 0; k < 4; k++) { h = Math.imul(h ^ (h >>> 13), 0x5bd1e995) >>> 0; out += ("0000000" + h.toString(16)).slice(-8); }
    return out;
  }
  function tick() { S.t += 1; return "T+" + S.t; }
  function log(cls, msg) {
    var p = document.createElement("p");
    p.innerHTML = '<span class="t">' + tick() + '</span><span class="' + cls + '">' + msg + "</span>";
    logEl.appendChild(p);
    logEl.scrollTop = logEl.scrollHeight;
  }
  function setStage(i, cls, note) {
    stages.forEach(function (el, j) {
      el.classList.remove("active", "done", "blocked");
      if (j < i) el.classList.add("done");
      if (j === i && cls) el.classList.add(cls);
    });
    if (note && stages[i]) stages[i].querySelector(".ss").textContent = note;
  }
  function blastPct() { return S.blast / ORG_REPOS; }
  function render() {
    planEl.textContent = plans[S.plan % plans.length];
    var h = fnv(plans[S.plan % plans.length]);
    S.hash = h;
    hashEl.textContent = "plan-sha256: " + h.slice(0, 16) + "…";
    hashEl.classList.toggle("stale", !!(S.approvedHash && S.approvedHash !== h));
    apprEl.textContent = S.approvedHash
      ? (S.approvedHash === h ? "approved for this plan" : "approval was for " + S.approvedHash.slice(0, 16) + "… (does not count)")
      : "no approval recorded";
    blastVal.textContent = S.blast + " of " + ORG_REPOS + " repos (" + Math.round(blastPct() * 100) + "%)";
    meterFill.style.width = Math.min(100, blastPct() * 100) + "%";
    meter.classList.toggle("over", blastPct() > 0.25);
    $("b-approve").disabled = S.phase !== "awaiting";
    $("b-hold").disabled = S.phase !== "awaiting";
    $("b-execute").disabled = !(S.phase === "awaiting" || S.phase === "approved");
    $("b-replan").disabled = !(S.phase === "awaiting" || S.phase === "approved");
    $("b-run").disabled = S.phase !== "idle";
  }
  function say(msg) { verdict.textContent = msg; }
  function step(fn, delay) { S.timers.push(setTimeout(fn, reduced ? 30 : delay)); }

  function reset() {
    (S ? S.timers : []).forEach(clearTimeout);
    S = { phase: "idle", plan: 0, hash: "", approvedHash: null, blast: parseInt(blastInput.value, 10) || 6, t: 0, timers: [] };
    logEl.innerHTML = "";
    stages.forEach(function (el) { el.classList.remove("active", "done", "blocked"); var ss = el.querySelector(".ss"); ss.textContent = ss.getAttribute("data-idle"); });
    log("si", "ion is idle. Reads are free. Writes are not.");
    say("Press run. Ion will read the whole org, write a plan, and stop at the gate. The gate is yours.");
    render();
  }

  function run() {
    if (S.phase !== "idle") return;
    S.phase = "running"; render();
    setStage(0, "active", "one batched GraphQL query, local git mirrors");
    log("si", "collect: metadata for " + ORG_REPOS + " repos, mirrors refreshed. No writes.");
    step(function () {
      setStage(1, "active", "health, policy drift, coupling, cohesion, lifecycle");
      log("si", "analyze: widget-docs and widget share commits, code and links. Coupling 0.71, over the 0.6 merge threshold.");
    }, 900);
    step(function () {
      setStage(2, "active", "scored, persisted 2 runs, plan written by the executor");
      log("si", "propose: [P-0007] filed in the private tracker with evidence, exact commands, rollback and its plan hash.");
    }, 1800);
    step(function () {
      S.phase = "awaiting";
      setStage(3, "active", "waiting: label, signed bus message, or dashboard tick");
      log("hu", "approve: the machine stops here. A maintainer decides, and only for the plan above.");
      say("Ion has done all it may do alone. Approve this exact plan, hold it, or change the plan and watch the approval rule bite.");
      render();
    }, 2700);
  }

  function approve() {
    if (S.phase !== "awaiting") return;
    S.approvedHash = S.hash; S.phase = "approved";
    setStage(3, "done", "maintainer approved this plan hash");
    log("hu", "maintainer added ion:approved. Timeline actor checked. Approval bound to " + S.hash.slice(0, 16) + "…");
    say("Approved, for this plan and no other. Execute when ready, or change the plan first and see what happens to that approval.");
    render();
  }
  function hold() {
    if (S.phase !== "awaiting") return;
    S.phase = "held";
    setStage(3, "blocked", "on hold by a maintainer");
    log("hu", "maintainer added ion:hold. The proposal waits. Reset the model to try another path.");
    say("Held. Nothing runs, nothing is lost. In the real system the proposal simply waits for a later decision.");
    render();
  }
  function replan() {
    if (!(S.phase === "awaiting" || S.phase === "approved")) return;
    var wasApproved = S.approvedHash === S.hash;
    S.plan += 1; S.phase = "awaiting";
    setStage(2, "active", "re-planned: commands changed, new hash");
    log("si", "propose: new evidence changed the plan. The hash changed with it.");
    if (wasApproved) log("no", "approve: the old approval was for a different hash. ion:approved comes off the issue. Approval is asked again.");
    step(function () { setStage(3, "active", "waiting again: the plan on display is new"); render(); }, 700);
    say("The plan changed, so its fingerprint changed. Any earlier yes was a yes to different commands, and ion treats it as no answer at all.");
    render();
  }
  function execute() {
    if (!(S.phase === "awaiting" || S.phase === "approved")) return;
    if (S.approvedHash !== S.hash) {
      setStage(4, "blocked", "refused at the gate");
      log("no", "execute: refused. No verified approval for the current plan hash. A database flag would not be enough either; the gate re-derives approval from the record.");
      say("Refused, and rightly. The executor checks the approval itself, every time, even for a dry run.");
      return;
    }
    if (blastPct() > 0.25) {
      setStage(4, "blocked", "circuit breaker tripped");
      log("no", "execute: circuit breaker. This run would touch " + Math.round(blastPct() * 100) + "% of the org, over the 25% guard. Aborted before any write.");
      say("Even with approval, a run that touches too much at once stops itself. Lower the blast radius and execute again.");
      return;
    }
    S.phase = "executing";
    setStage(4, "active", "typed writes only, capped per run, every step ledgered");
    log("si", "execute: gate passed for " + S.hash.slice(0, 16) + "… Dry-run checks done. Writing through typed requests (cap: 5 PRs per run).");
    step(function () { log("si", "merge verified exactly: target tree equals source tree, source tip is an ancestor. Commits carry Ion-Origin trailers."); }, 1000);
    step(function () {
      S.phase = "done";
      setStage(5, "done", "dashboard updated, digest sent");
      stages[4].classList.add("done");
      log("ok", "report: Org health issue updated in place, digest sent. A maintainer merges the PR. Ion never merges its own.");
      say("Done, in the only order ion allows: read widely, propose plainly, wait for a person, then write narrowly and prove it.");
      render();
    }, 2000);
    render();
  }

  $("b-run").addEventListener("click", run);
  $("b-approve").addEventListener("click", approve);
  $("b-hold").addEventListener("click", hold);
  $("b-replan").addEventListener("click", replan);
  $("b-execute").addEventListener("click", execute);
  $("b-reset").addEventListener("click", reset);
  blastInput.addEventListener("input", function () {
    S.blast = parseInt(blastInput.value, 10) || 0;
    if (blastPct() > 0.25) log("no", "guard check: " + S.blast + " repos is over a quarter of the org. An --apply run would trip the breaker.");
    render();
  });
  reset();
})();
