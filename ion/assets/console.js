'use strict';
(() => {
const data = JSON.parse(document.getElementById('ion-data').textContent);
const root = document.getElementById('view');
const arr = value => Array.isArray(value) ? value : [];
const str = value => value == null ? 'Unknown' : String(value);
const pretty = value => JSON.stringify(value == null ? {} : value, null, 2);
const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = str(text); if (cls) n.className = cls; return n; };
const add = (parent, ...children) => { children.forEach(child => parent.append(child)); return parent; };
const badge = value => el('span', str(value).replaceAll('_', ' '), 'badge ' + (/^[a-z-]+$/.test(str(value)) ? value : ''));
const date = value => value ? str(value).replace('T', ' ').replace(/\.\d+Z$/, ' UTC') : 'Not recorded';
function link(url, label) {
  if (typeof url !== 'string' || !/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(?:\/(?:issues|pull)\/[1-9][0-9]*)?\/?$/.test(url)) return el('span', label + (url ? ' (link unavailable; see raw evidence)' : ''));
  const n = el('a', label); n.href = url; n.rel = 'noopener noreferrer'; n.target = '_blank'; return n;
}
function empty(parent, title, detail) { add(parent, add(el('div', null, 'empty'), el('strong', title), el('p', detail))); }
function heading(title, subtitle, count) { const n = el('div', null, 'section-head'); add(n, add(el('div'), el('h2', title), el('p', subtitle, 'muted'))); if(count != null) n.append(el('span', count + ' RECORDS', 'count')); root.append(n); }
function fact(label, value) { return add(el('div', null, 'fact'), el('small', label), el('div', value)); }
function raw(parent, title, value) { const d = el('details'); add(d, add(el('summary'), el('strong', title)), el('pre', typeof value==='string'?value:pretty(value))); parent.append(d); }
function detail(title, subtitle, status) { const d = el('details', null, 'item'); const s = el('summary'); add(s, add(el('div', null, 'title'), el('strong', title), el('p', subtitle)), badge(status)); const body = el('div', null, 'dossier'); add(d, s, body); return {d, body}; }
function overview() {
 heading('The organisation, in view.', 'Saved evidence for a considered next move.');
 const s = data.summary || {}, metrics = el('div', null, 'metrics');
 [['Repositories','repositories'],['Active repositories','active_repositories'],['Health findings','health_findings'],['Policy drift','policy_drift'],['Awaiting approval','awaiting_approval'],['Open proposals','open_proposals']].forEach(([label,key]) => add(metrics, add(el('div', null, 'metric'), el('b', s[key] == null ? '—' : s[key]), el('span', label)))); root.append(metrics);
 const grid=el('div',null,'grid'), readiness=data.readiness||{}, fresh=data.freshness||{}, safety=data.safety||{};
 const panel=add(el('section',null,'panel accent'), el('p','EVIDENCE READINESS','kicker'), el('h3','What needs attention'),badge(readiness.status||'empty'));
 if(!arr(readiness.checks).length) empty(panel,'No readiness evidence','Collect and analyse an organisation to establish its state.');
 arr(readiness.checks).forEach(c => add(panel,add(el('div',null,'check'),badge(c.status||'unknown'),el('strong',c.label||c.id),el('p',c.detail))));
 const other=add(el('section',null,'panel'),el('p','OPERATING BOUNDARY','kicker'),el('h3','Evidence, then permission'),el('p','This console has no execution authority. Approval remains in the organisation’s existing verified channels.','muted'));
 add(other,add(el('div',null,'row'),el('strong','Kill switch'),el('p',safety.paused===true?'Paused · execution stopped':safety.paused===false?'Not paused · approval gates still apply':'Unknown')),
 add(el('div',null,'row'),el('strong','Source freshness'),badge(fresh.status||'unknown'),el('p',fresh.reason||'No collection is available.'),el('p','Last complete successful pipeline: '+date(fresh.last_success_at))),
 add(el('div',null,'row'),el('strong','Analysis generation'),el('p',str((data.analysis||{}).generation)+' / collection '+str((data.collection||{}).generation))));
 if(safety.breaker) add(other,el('div','Circuit breaker: '+str(safety.breaker.reason||pretty(safety.breaker)),'notice'));
 add(grid,panel,other); root.append(grid);
}
function repository(r) {
 const {d,body}=detail(r.name||'Unnamed repository',r.description||'No description recorded',r.archived?'archived':r.lifecycle||'unknown');
 const tags=el('div',null,'tags'); [r.private?'private':'public',r.fork?'fork':null,...arr(r.topics)].filter(Boolean).forEach(t=>tags.append(badge(t)));
 add(body,tags,el('p'),link(r.url,'Open repository on GitHub ↗'),add(el('div',null,'facts'),fact('Default branch',r.default_branch),fact('Last push',date(r.pushed_at)),fact('Mirror',str((r.mirror||{}).status)+' · '+str((r.mirror||{}).detail)),fact('Policy governance',r.governed==null?'Unknown':r.governed?'Governed':'Excluded')));
 ['health','policy'].forEach(key=>{add(body,el('h3',key==='health'?'Health findings':'Policy evidence')); if(!arr(r[key]).length) add(body,el('p','No recorded findings.','muted')); arr(r[key]).forEach(f=>add(body,add(el('div',null,'row'),badge(f.status||f.code||f.class),el('p',f.detail||pretty(f)))));}); raw(body,'Complete repository evidence',r); return d;
}
function proposal(p) {
 const {d,body}=detail((p.id||'Proposal')+' · '+(p.title||p.kind||'Untitled'),str(p.kind)+' / '+arr(p.repos).join(', '),p.status||'unknown');
 const approval=p.approval||{},plan=p.plan||{};
 add(body,el('p'),link(p.issue_url,'Review proposal on GitHub ↗'),add(el('div',null,'facts'),fact('Recorded approval evaluation',str(approval.state)+' · '+str(approval.reason)),fact('Approvers',arr(approval.approvers).join(', ')||'None recorded'),fact('Plan SHA-256',p.plan_hash||'No plan hash recorded'),fact('Last updated',date(p.updated))));
 add(body,el('h3','Regenerated current phase plan'),el('p','Generated locally from saved evidence. This plan is not a live approval or an execution authorization.','muted'));
 if(plan.hash_matches===false) add(body,el('div','Plan hash mismatch: this regenerated phase plan differs from the stored approval hash. Do not treat it as approved.','notice'));
 add(body,add(el('div',null,'facts'),fact('Current phase',plan.phase||'Not recorded'),fact('Generated fingerprint',plan.fingerprint||'Not available'),fact('Stored hash comparison',plan.hash_matches===true?'Matches stored hash':plan.hash_matches===false?'Does not match stored hash':'Unknown')));
 if(plan.error) add(body,el('p',plan.error,'notice'));
 if(!arr(plan.steps).length) add(body,el('p','No plan steps recorded.','muted'));
 arr(plan.steps).forEach(step=>{const n=add(el('div',null,'row'),el('strong',step.name),el('p',step.description)); arr(step.commands).forEach(c=>n.append(el('pre',Array.isArray(c)?c.join(' '):c,'command'))); body.append(n);});
 add(body,el('h3','Rollback'),el('pre',arr(plan.rollback).join('\n')||'No rollback notes recorded.'));
 raw(body,'Saved tracking issue body (plain text)',p.recorded_body||'No saved issue body recorded.');
 raw(body,'Evidence and rationale',{evidence:p.evidence,text:p.text,subject:p.subject}); raw(body,'Changesets and execution ledger',{changesets:p.changesets,ledger:p.ledger}); raw(body,'Complete proposal record',p); return d;
}
function metricSummary(metrics) {
 const m=metrics||{};
 return [['GraphQL points','graphql_points'],['REST calls','rest_calls'],['Writes succeeded','writes_succeeded'],['Writes failed','writes_failed']].map(([label,key])=>label+': '+(m[key]==null?'not recorded':m[key])).join(' · ');
}
function run(r) {
 const {d,body}=detail('Run '+str(r.id),date(r.started)+' / '+(r.mode==='dry-run'?'Executor dry run':str(r.mode))+' · '+metricSummary(r.metrics),r.status||'unknown');
 add(body,add(el('div',null,'facts'),fact('Started',date(r.started)),fact('Finished',date(r.finished)),fact('Duration',r.duration_seconds==null?'Not recorded':r.duration_seconds+' seconds'),fact('Mode',r.mode==='dry-run'?'Executor dry run':r.mode)));
 add(body,el('p',metricSummary(r.metrics),'metric-summary'));
 if(r.mode==='dry-run') add(body,el('p','Executor dry run: proposal tracking, approval synchronization and report delivery may still publish to GitHub or configured notifiers.','notice'));
 if(arr(r.errors).length) add(body,el('h3','Run errors'),el('pre',arr(r.errors).map(str).join('\n')));
 add(body,el('h3','Pipeline steps'));
 if(!arr(r.steps).length) add(body,el('p','No stage records are available.','muted'));
 arr(r.steps).forEach(s=>{const row=el('details'); add(row,add(el('summary'),add(el('div',null,'title'),el('strong',s.name),el('p',metricSummary(s.metrics))),badge(s.status||'unknown')),el('pre',pretty(s))); body.append(row);}); raw(body,'Complete run evidence',r); return d;
}
function listing(kind) {
 const items=arr(data[kind]), names={repositories:['Repositories','The fleet, with its findings and boundaries.'],proposals:['Proposals','Inspect the evidence and the exact plan before deciding.'],runs:['Run history','Collection is one stage. Follow the whole pipeline.']};
 heading(...names[kind],items.length);
 const toolbar=el('div',null,'toolbar'), search=el('input'); search.type='search'; search.placeholder='Search '+kind+'…';
 add(toolbar,add(el('label',null,'field'),el('span','Search saved evidence'),search));
 const selects=[]; const filterKeys=kind==='proposals'?['status','kind']:['status'];
 filterKeys.forEach(key=>{const select=el('select'); add(select,el('option','All '+(key==='kind'?'kinds':'states'))); select.options[0].value=''; const values=[...new Set(items.map(item=>key==='status'&&kind==='repositories'?(item.archived?'archived':item.lifecycle||'unknown'):item[key]||'unknown'))].sort(); values.forEach(v=>{const o=el('option',v);o.value=v;select.append(o);}); add(toolbar,add(el('label',null,'field'),el('span',key==='kind'?'Proposal kind':'Status'),select)); selects.push([key,select]);}); root.append(toolbar);
 const count=el('p',null,'count'),list=el('div',null,'list'); add(root,count,list);
 function update(){const needle=search.value.toLocaleLowerCase(); const filtered=items.filter(item=>(!needle||pretty(item).toLocaleLowerCase().includes(needle))&&selects.every(([key,s])=>!s.value||s.value===(key==='status'&&kind==='repositories'?(item.archived?'archived':item.lifecycle||'unknown'):item[key]||'unknown'))); count.textContent=filtered.length+' of '+items.length+' records';list.replaceChildren(); if(!filtered.length) empty(list,items.length?'No matching records':'No '+kind+' yet',items.length?'Try another search or clear the filters.':'This snapshot contains no '+kind+'. Saved evidence will appear here after a local run.');filtered.forEach(item=>list.append(({repositories:repository,proposals:proposal,runs:run})[kind](item))); }
 search.addEventListener('input',update);selects.forEach(([,s])=>s.addEventListener('change',update));update();
}
function navigate(view,focus=false){root.replaceChildren(); document.querySelectorAll('[data-view]').forEach(b=>{if(b.dataset.view===view)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});if(view==='overview')overview();else listing(view);if(focus)document.getElementById('main').focus();}
document.getElementById('org').textContent=data.org||'Your organisation';document.getElementById('generated').textContent='Saved '+date(data.generated_at)+' · '+str((data.freshness||{}).status||'No collection');document.getElementById('demo').hidden=!data.demo;
document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>navigate(b.dataset.view,true)));
document.getElementById('download').addEventListener('click',()=>{const url=URL.createObjectURL(new Blob([pretty(data)],{type:'application/json'}));const a=el('a');a.href=url;a.download='ion-evidence.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});navigate('overview');
})();
