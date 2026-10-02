const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const title = value => String(value).toLowerCase().replace(/(^|\s)\S/g, c => c.toUpperCase());
const number = n => Number(n || 0).toLocaleString();
const seconds = ms => `${(Number(ms || 0) / 1000).toFixed(1)}s`;
const formatDate = value => new Date(value).toLocaleString(undefined, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
const symbols = ['◎', '◇', '✓', '≋'];
const colors = ['#6b9d82','#abc4b4','#cbd9cf','#d7b57b','#cf9581','#b75e69'];
let config, corpus = [], runList = [], activeRun = null, currentCase = null;
let runId = localStorage.getItem('reviewer-run') || '', page = 'overview', detailTab = 'conversation';
const emptyFilters = () => ({search:'', scope:'', resolution:'', compliance:'', factuality:'', communication:''});
let filters = emptyFilters();
let busy = false, routeVersion = 0;

async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request could not be validated. Check the input and try again.');
  return data;
}
function toast(message) {
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $('#toast').hidden = true, 6000);
}
function good(value) { return config.passing.includes(value); }
function tone(value) { return good(value) ? 'good' : value.includes('CRITICAL') || value.includes('CONTRADICTED') || value.includes('FALSELY') ? 'critical' : ['PARTIALLY RESOLVED','NEEDS IMPROVEMENT','INACCURATE','NON-COMPLIANT'].includes(value) ? 'warning' : 'bad'; }
function badge(value) { return `<span class="badge ${value ? tone(value) : ''}"><span class="badge-dot"></span>${esc(value ? title(value) : 'Not evaluated')}</span>`; }
function outcomeColor(label) { return good(label) ? label==='CORRECTLY REFUSED'?colors[1]:label==='CORRECTLY ESCALATED'?colors[2]:colors[0] : tone(label) === 'critical' ? '#b86673' : tone(label) === 'warning' ? '#d1b080' : '#ce958b'; }
function runPicker() {
  return `<select class="run-picker" aria-label="Select evaluation run"><option value="">Dataset · ${corpus.length} cases</option>${runList.map(r => `<option value="${esc(r.id)}" ${r.id===runId?'selected':''}>${r.mode==='collection'?'Collection':'Run'} ${r.id.slice(0,6)} · ${esc(formatDate(r.created_at))}</option>`).join('')}</select>`;
}
function header(name, subtitle) {
  return `<div class="page-heading"><div><h1>${name}</h1>${subtitle ? `<p>${subtitle}</p>` : ''}</div><div class="heading-actions"><button class="button primary" id="new-run-button">＋ New evaluation</button></div></div>`;
}
function runStrip() {
  const done = activeRun?.counts.completed || 0;
  const info = activeRun ? `${activeRun.mode==='collection'?'Collected evaluations':'Retail evaluation run'} <span class="dot-separator">/</span> ${activeRun.id.slice(0,6)}` : 'τ³ Retail dataset';
  return `<div class="run-strip"><div class="run-info"><div><div class="run-title">${info}</div><div class="run-meta">${activeRun?esc(formatDate(activeRun.created_at)):'50 recorded customer interactions'}<span class="dot-separator">·</span>${activeRun?`${done} of ${activeRun.total_cases} reviewed`:'Ready for independent evaluation'}${activeRun?'':'<span class="dot-separator">·</span>10 coverage categories'}</div>${activeRun?.status==='running'?`<div class="progress"><div style="width:${100*(done+activeRun.counts.failed)/activeRun.total_cases}%"></div></div>`:''}</div></div><div class="heading-actions"><span class="status ${activeRun?.status || ''}">${activeRun?title(activeRun.status):'Ready'}</span>${runPicker()}</div></div>`;
}
function runNotice() {
  if (activeRun?.can_resume) return `<div class="notice"><p>${esc(activeRun.progress || 'This run was interrupted. Completed checks are saved.')} <button class="text-button" id="resume-run" ${runList.some(r=>r.status==='running')?'disabled':''}>Resume unfinished checks ↗</button></p></div>`;
  if (activeRun?.status==='running' && activeRun.progress) return `<div class="notice" role="status"><p>${esc(activeRun.progress)}</p></div>`;
  if (activeRun?.mode === 'collection') return '<div class="notice"><p><strong>Collected evaluations</strong> · These saved reviews were evaluated one conversation at a time. Usage totals describe the original calls; collecting them made no additional API calls. Individual attempts remain in the run history.</p></div>';
  if (activeRun?.counts.failed) return `<div class="notice error"><p>${activeRun.counts.failed} evaluation${activeRun.counts.failed===1?'':'s'} failed. Distributions include only completed reviews. Open Results to inspect errors.</p></div>`;
  if (!activeRun) return `<div class="notice"><span>◈</span><p><strong>Your dataset is ready.</strong> Explore 50 real τ³ Retail conversations, or start an evaluation to see quality signals.${!config.api_key_configured?' Configure your API key in .env for live reviews.':''}</p></div>`;
  return '';
}
function rows() {
  return activeRun ? activeRun.results : corpus.map(c => ({...c, case_id:c.id, evaluation:null, status:'not evaluated'}));
}
function matchesFilters(row) {
  if (filters.search && ![row.case_id,row.scenario,row.objective,...row.tags].join(' ').toLowerCase().includes(filters.search.toLowerCase())) return false;
  if (filters.scope==='reviewed' && !row.evaluation) return false;
  if (filters.scope==='attention' && !config.framework.some(f=>row.evaluation?.[f.key] && !good(row.evaluation[f.key].result))) return false;
  return config.framework.every(f=>!filters[f.key] || (filters[f.key]==='flagged' ? row.evaluation?.[f.key] && !good(row.evaluation[f.key].result) : row.evaluation?.[f.key].result===filters[f.key]));
}
function openResults(selection) {
  filters={...emptyFilters(),...selection};
  if(location.hash==='#results')resultPage();
  else location.hash='results';
}
function attentionChart(reviewed) {
  return `<section class="attention-panel" aria-labelledby="attention-title"><div class="section-heading"><h2 id="attention-title">Where cases need attention</h2><span>${reviewed.length} reviewed cases</span></div><p class="muted small">A case can need attention in more than one check.</p><div class="attention-bars">${config.framework.map(f=>{
    const count=reviewed.filter(r=>!good(r.evaluation[f.key].result)).length;
    const percent=reviewed.length?count/reviewed.length*100:0;
    return `<button type="button" class="attention-row" data-results="${esc(JSON.stringify({[f.key]:'flagged'}))}" ${reviewed.length?'':'disabled'} aria-label="${esc(f.name)}: ${count} of ${reviewed.length} reviewed cases need attention"><span class="attention-label">${esc(f.short)}</span><span class="attention-track" aria-hidden="true"><span style="width:${percent}%"></span></span><span class="attention-value">${reviewed.length?`${count} / ${reviewed.length} · ${percent.toLocaleString(undefined,{maximumFractionDigits:1})}%`:'—'} <span aria-hidden="true">↗</span></span></button>`;
  }).join('')}</div>${reviewed.length?'':'<p class="muted small">Complete reviews to see which checks need attention.</p>'}</section>`;
}
function overview() {
  const reviewed = rows().filter(r => r.evaluation);
  const completed = reviewed.length;
  const total = activeRun?.total_cases ?? corpus.length;
  const percent = (count, denominator) => denominator ? `${(count/denominator*100).toLocaleString(undefined,{maximumFractionDigits:1})}%` : '—';
  const flagged = reviewed.filter(r => Object.values(r.evaluation).some(s => !good(s.result))).length;
  const critical = reviewed.filter(r => r.evaluation.compliance.result==='CRITICAL NON-COMPLIANCE').length;
  const stats = [
    ['Conversations', total, activeRun?'Included in this run':'Real τ³ Retail cases', {}],
    ['Reviewed', completed, `${percent(completed,total)} of ${total} conversations`, {scope:'reviewed'}],
    ['Needs attention', completed?flagged:'—', completed?`${percent(flagged,completed)} of reviewed cases`:'No completed reviews', {scope:'attention'}],
    ['Critical policy issues', completed?critical:'—', completed?`${percent(critical,completed)} of reviewed cases`:'No completed reviews', {compliance:'CRITICAL NON-COMPLIANCE'}],
  ];
  $('#main').innerHTML = header('Review overview') + runStrip() + runNotice() +
    `<div class="summary-grid">${stats.map(s => `<button type="button" class="stat-card" data-results="${esc(JSON.stringify(s[3]))}" aria-label="View ${s[0].toLowerCase()}: ${s[1]}"><span class="stat-label">${s[0]}<span aria-hidden="true">↗</span></span><span class="stat-value">${s[1]}</span><span class="stat-note">${s[2]}</span></button>`).join('')}</div>` + attentionChart(reviewed) +
    `<div class="section-heading"><h2>Evaluation results</h2><span>${completed?`${completed} completed reviews`:'No aggregate quality score'}</span></div><div class="chart-grid">${config.framework.map((f,i) => {
      const distribution = activeRun?.distributions[f.key] || Object.fromEntries(Object.keys(f.outcomes).map(label => [label,0]));
      const passingCount = Object.entries(distribution).reduce((sum,[label,count]) => sum + (good(label)?count:0),0);
      return `<section class="chart-card"><div class="chart-heading"><div class="chart-title"><span class="signal-icon">${symbols[i]}</span><h3>${f.name}</h3></div></div><p class="chart-question">${f.question}</p><div class="chart-number"><strong>${completed?`${(passingCount/completed*100).toLocaleString(undefined,{maximumFractionDigits:1})}%`:'—'}</strong><span>${['appropriate outcomes','policy compliant','factually correct','appropriately communicated'][i]}</span></div><div class="stack" role="img" aria-label="${esc(f.name)} outcome distribution">${Object.entries(distribution).filter(([,n])=>n).map(([label,count],j)=>`<div class="segment" style="width:${count/completed*100}%;background:${outcomeColor(label,j)}" title="${esc(label)}: ${count}"></div>`).join('')}</div><div class="legend">${Object.entries(distribution).map(([label,count],j)=>`<button type="button" class="legend-row" data-results="${esc(JSON.stringify({[f.key]:label}))}" aria-label="View ${esc(f.short)} results: ${esc(title(label))}"><span class="swatch" style="background:${outcomeColor(label,j)}"></span><span class="legend-name">${esc(title(label))}</span><span class="legend-count">${completed?count:'—'}</span><span class="legend-percent">${completed?`${(count/completed*100).toLocaleString(undefined,{maximumFractionDigits:1})}%`:'—'}</span></button>`).join('')}</div><button class="chart-link" data-signal="${f.key}">Inspect ${f.short.toLowerCase()} results <span>↗</span></button></section>`;
    }).join('')}</div>` + metricsPanel();
}
function metricsPanel() {
  const live = activeRun && ['openai', 'collection'].includes(activeRun.mode);
  const m = activeRun?.metrics;
  const metrics = [
    ['Model', live?activeRun.config.model:'—', live?'Requested model':'No live inference'],
    ['Input tokens', live?number(m.input_tokens):'—', live?`${number(m.cached_tokens)} cached tokens`:'Awaiting API usage'],
    ['Output tokens', live?number(m.output_tokens):'—', 'Includes reasoning, when present'],
    ['Total tokens', live?number(m.total_tokens):'—', 'Input + output'],
    ['Conversations evaluated', live?activeRun.counts.completed:'—', live?`${activeRun.total_cases} selected`:'Completed live reviews'],
    ['Average latency', live?seconds(m.average_latency_ms):'—', 'Per completed or failed attempt'],
    ['Total latency', live?seconds(m.latency_ms):'—', 'Check durations, including API retry waits'],
    ['Estimated API cost', live && m.estimated_cost!==null?`$${m.estimated_cost.toFixed(4)}`:'—', live && m.estimated_cost===null?'Pricing or usage unavailable':'USD · configured base token rates'],
  ];
  return `<div class="section-heading"><h2>Evaluation observability</h2></div><section class="metrics-panel"><div class="metrics-top"><h3>Inference & usage</h3></div><div class="metrics-grid">${metrics.map(([label,value,note])=>`<div><div class="metric-label">${label}</div><div class="metric-value">${esc(value)}</div><div class="metric-note">${esc(note)}</div></div>`).join('')}</div></section>`;
}
function resultPage() {
  $('#main').innerHTML = header('Conversation results') + runStrip() + runNotice() +
    `<div class="filters"><div class="search-wrap"><span>⌕</span><input id="search" type="search" aria-label="Search cases" placeholder="Search case, scenario, or objective…" value="${esc(filters.search)}"></div><select class="filter-select" data-filter="scope" aria-label="Filter review status"><option value="">All conversations</option><option value="reviewed" ${filters.scope==='reviewed'?'selected':''}>Reviewed</option><option value="attention" ${filters.scope==='attention'?'selected':''}>Needs attention</option></select>${config.framework.map(f=>`<select class="filter-select" data-filter="${f.key}" aria-label="Filter ${esc(f.name)}"><option value="">All ${f.short.toLowerCase()}</option><option value="flagged" ${filters[f.key]==='flagged'?'selected':''}>Needs attention</option>${Object.keys(f.outcomes).map(label=>`<option ${filters[f.key]===label?'selected':''} value="${esc(label)}">${esc(title(label))}</option>`).join('')}</select>`).join('')}<button class="text-button" id="clear-filters">Reset</button></div><div id="results-table"></div>`;
  renderTable();
}
function scenarioTags(row) {
  const [first, ...extra] = [...new Set([row.scenario, ...row.tags])];
  const tag = value => `<span class="badge scenario-badge">${esc(value)}</span>`;
  if (!extra.length) return tag(first);
  return `<details class="scenario-tags"><summary aria-label="${esc(first)}; ${extra.length} additional scenarios">${tag(first)}<span class="badge scenario-badge scenario-count">+${extra.length}</span></summary><div class="scenario-extra">${extra.map(tag).join('')}</div></details>`;
}
function renderTable() {
  const all = rows();
  const visible = all.filter(matchesFilters);
  $('#results-table').innerHTML = `<div class="filter-count">${visible.length} of ${all.length} conversations</div><div class="table-card"><div class="table-scroll"><table><thead><tr><th>CONVERSATION</th><th>SCENARIO</th><th>RESOLUTION</th><th>POLICY</th><th>FACTUALITY</th><th>COMMUNICATION</th><th></th></tr></thead><tbody>${visible.map(row=>`<tr><td><a class="case-link" href="#case/${encodeURIComponent(row.case_id)}">${esc(row.case_id)}</a>${['failed','running','pending'].includes(row.status)?`<div class="case-sub">${row.status==='failed'?'Review failed · inspect error':row.status==='running'?`Reviewing · ${row.completed_checks || 0}/4 checks complete`:row.error?esc(row.error):'Queued for evaluation'}</div>`:''}</td><td>${scenarioTags(row)}</td>${config.framework.map(f=>`<td>${row.evaluation?badge(row.evaluation[f.key].result):`<span class="badge">${esc(title(row.status))}</span>`}</td>`).join('')}<td><a href="#case/${encodeURIComponent(row.case_id)}" aria-label="Inspect ${esc(row.case_id)}">↗</a></td></tr>`).join('')}</tbody></table>${visible.length?'':'<div class="empty"><h3>No conversations match</h3><p>Try a different outcome or reset your filters.</p></div>'}</div></div>`;
}
function methodology() {
  $('#main').innerHTML = `<div class="page-heading"><div><h1>Methodology</h1></div></div><section class="method-intro"><h2>τ³ provides the scenario and system truth.<br>Reviewer independently evaluates quality.</h2><p>We retain customer objectives, business policy, transcripts, tool activity, and initial and final states. Benchmark rewards, pass/fail outcomes, reference actions, and quality judgments are excluded from the review input.</p><p>New evaluations use four separate prompts and API calls per conversation. Each check receives the original evidence and its own rubric, without seeing the other checks’ judgments.</p></section><div class="method-grid">${config.framework.map((f,i)=>`<section class="method-card"><span class="signal-icon">${symbols[i]}</span><h2>${f.name}</h2><p>${f.question}</p><div class="method-evidence"><strong>Evidence used</strong><br>${f.evidence}</div>${Object.entries(f.outcomes).map(([label,definition])=>`<div class="outcome">${badge(label)}<p>${definition}</p></div>`).join('')}<button type="button" class="text-button prompt-link" data-prompt="${f.key}" aria-haspopup="dialog" aria-controls="prompt-dialog">View evaluation prompt ↗</button></section>`).join('')}</div><section class="panel"><div class="eyebrow">DATASET PROVENANCE</div><h2>50 recorded τ³ Retail cases</h2><p class="muted small" style="margin-top:12px">These 50 conversations cover different customer scenarios from a public dataset, regardless of benchmark scores. We recreate the final system state with official Retail tools and check their outputs against recorded results, except calculator outputs. All relevant and changed records are kept. Keyword-based tags can overlap.</p><div class="coverage-tags">${Object.entries(config.dataset.coverage).map(([tag,count])=>`<span>${esc(tag)} · ${count}</span>`).join('')}</div><div class="provenance provenance-links"><a href="${esc(config.dataset.source)}" target="_blank" rel="noreferrer">View source package ↗</a><button type="button" class="text-button" id="inspect-raw-case" aria-haspopup="dialog" aria-controls="raw-case-dialog">Inspect case raw data</button></div></section>`;
}
function conversation(caseData) {
  const toolHtml = call => `<details class="tool-event"><summary>⌘ ${esc(call.name)} ${call.error?'· Failed':'· Tool response'}</summary><div class="source-label" style="margin-top:14px">ARGUMENTS</div><pre>${esc(JSON.stringify(call.arguments,null,2))}</pre><div class="source-label">RESULT</div><pre>${esc(typeof call.result==='string'?call.result:JSON.stringify(call.result,null,2))}</pre></details>`;
  return caseData.tool_calls.filter(c=>c.after_message===-1).map(toolHtml).join('') + caseData.transcript.map((m,i)=>`<div class="message ${m.role}"><div class="message-label">${m.role==='user'?'CUSTOMER':'AGENT'} <span class="muted">· ${i+1}</span></div><div class="message-content">${esc(m.content)}</div></div>${caseData.tool_calls.filter(c=>c.after_message===i).map(toolHtml).join('')}`).join('');
}
function detailContent() {
  const c = currentCase.case;
  if (detailTab==='conversation') return conversation(c);
  if (detailTab==='policy') return `<div class="policy">${esc(c.policy)}</div>`;
  return `<div class="source-label">INITIAL STATE</div><pre>${esc(JSON.stringify(c.initial_state,null,2))}</pre><div class="source-label">FINAL STATE</div><pre>${esc(JSON.stringify(c.final_state,null,2))}</pre>`;
}
function detailPage() {
  const c = currentCase.case, evaluation = currentCase.evaluation;
  $('#main').innerHTML = `<a class="back-link" href="#results">← Back to results</a><div class="detail-heading"><div class="eyebrow">CONVERSATION REVIEW</div><h1>${esc(c.id)}</h1><p>${esc(c.scenario)}<span class="dot-separator">·</span>${esc(c.provenance.dataset)}<span class="dot-separator">·</span>${c.transcript.length} messages</p></div>${runNotice()}<div class="detail-layout"><section class="panel"><div class="source-label">CUSTOMER OBJECTIVE</div><p class="objective">${esc(c.objective)}</p><div class="tabs" role="tablist" aria-label="Case evidence">${[['conversation','Conversation'],['policy','Business policy'],['state','System state']].map(([key,label])=>`<button role="tab" aria-selected="${detailTab===key}" class="${detailTab===key?'active':''}" data-tab="${key}">${label}</button>`).join('')}</div><div id="case-evidence" role="tabpanel">${detailContent()}</div></section><aside class="panel"><div class="source-label">QUALITY SIGNALS</div>${evaluation?config.framework.map(f=>{const signal=evaluation[f.key]; return `<section class="signal-review"><h3>${f.name}</h3>${badge(signal.result)}${signal.severity!=='none'?`<span class="severity">${title(signal.severity)} severity</span>`:''}<p>${esc(signal.explanation)}</p>${signal.evidence.map((e,i)=>`<button class="evidence-link" data-evidence="${f.key}:${i}"><code>↗ ${esc(e.path)}</code><span>“${esc(e.quote)}”</span></button>`).join('')}</section>`;}).join(''):`<div class="empty"><h3>${currentCase.status==='failed'?'Evaluation failed':currentCase.status==='running'?'Evaluation in progress':'No evaluation yet'}</h3><p>${esc(currentCase.error || 'Start a review run to see independent judgments and supporting evidence.')}</p></div>`}</aside></div>`;
}
function showEvidence(key,index) {
  const evidence=currentCase.evaluation[key].evidence[index];
  let value=currentCase.case;
  for(const part of evidence.path.slice(1).split('/')) value=value[part.replace(/~1/g,'/').replace(/~0/g,'~')];
  const text=typeof value==='string'?value:JSON.stringify(value,null,2);
  $('#evidence-path').textContent=evidence.path;
  const offset=text.indexOf(evidence.quote);
  $('#evidence-source').innerHTML=offset<0?esc(text):esc(text.slice(0,offset))+`<mark>${esc(evidence.quote)}</mark>`+esc(text.slice(offset+evidence.quote.length));
  $('#evidence-dialog').showModal();
}
async function render() {
  const version=++routeVersion;
  const route=location.hash==='#prd'?'documentation':location.hash.slice(1) || 'overview';
  page=route.startsWith('case/')?'results':['overview','results','methodology','documentation'].includes(route)?route:'overview';
  document.querySelectorAll('[data-nav]').forEach(el=>{el.classList.toggle('active',el.dataset.nav===page); if(el.dataset.nav===page)el.setAttribute('aria-current','page'); else el.removeAttribute('aria-current');});
  $('#nav-count').textContent=activeRun?.total_cases ?? corpus.length;
  if(route.startsWith('case/')) {
    $('#main').innerHTML='<div class="loading">Loading conversation…</div>';
    const id=encodeURIComponent(decodeURIComponent(route.slice(5)));
    try {
      const result=await api(runId?`/api/runs/${runId}/cases/${id}`:`/api/cases/${id}`);
      if(version!==routeVersion)return;
      currentCase=result;
      detailTab='conversation';
      detailPage();
    } catch(error) {
      if(version===routeVersion)$('#main').innerHTML=`<div class="empty"><h3>Unable to open this conversation</h3><p>${esc(error.message)}</p><a class="text-button" href="#results">Back to results</a></div>`;
    }
  } else if(page==='overview')overview(); else if(page==='results')resultPage(); else if(page==='documentation')$('#main').innerHTML=$('#documentation-content').innerHTML; else methodology();
}
async function refreshRun() {
  runList=await api('/api/runs');
  if(runId && !runList.some(r=>r.id===runId))runId='';
  activeRun=runId?await api(`/api/runs/${runId}`):null;
  localStorage.setItem('reviewer-run',runId);
}
function newRun() {
  $('#configured-model').textContent=config.model;
  const price = value => value == null ? 'Not configured' : `$${Number(value).toLocaleString('en-US',{maximumFractionDigits:8})}`;
  $('#configured-input-price').textContent=price(config.pricing.input_price);
  $('#configured-cached-price').textContent=price(config.pricing.cached_input_price ?? config.pricing.input_price);
  $('#configured-output-price').textContent=price(config.pricing.output_price);
  $('#key-notice').textContent=config.api_key_configured?`Four independent checks per conversation. ${config.pacing.case_delay_seconds}s between conversations; temporary API rate limits retry automatically. This run makes billable API requests.`:'Add OPENAI_API_KEY to the project’s .env file and restart the server to enable live evaluations.';
  $('#start-run').disabled=!config.api_key_configured || runList.some(r=>r.status==='running');
  if(runList.some(r=>r.status==='running'))$('#key-notice').textContent='A run is in progress. Wait for it to complete before starting another.';
  $('#run-dialog').showModal();
}
document.addEventListener('click',async event=>{
  if(event.target.closest('.skip-link')){event.preventDefault();$('#main').focus();return;}
  const button=event.target.closest('button');
  if(!button)return;
  if(button.dataset.close)document.getElementById(button.dataset.close).close();
  if(button.id==='inspect-raw-case'){
    try {
      const {case: data}=await api(`/api/cases/${encodeURIComponent(corpus[0].id)}`);
      $('#raw-case-caption').textContent=`Dataset case · ${data.id}`;
      $('#raw-case-json').innerHTML=renderJson(data);
      $('#raw-case-dialog').showModal();
    } catch(error){toast(error.message);}
  }
  if(button.dataset.prompt){
    const criterion=config.framework.find(f=>f.key===button.dataset.prompt);
    $('#prompt-title').textContent=`${criterion.name} prompt`;
    $('#prompt-rubric').innerHTML=renderJson(criterion);
    $('#prompt-source').textContent=config.check_prompts[criterion.key];
    $('#prompt-full').open=true;
    $('#prompt-dialog').showModal();
  }
  if(button.id==='resume-run'){
    button.disabled=true;
    try {
      await api(`/api/runs/${runId}/resume`,{method:'POST'});
      await refreshRun();await render();toast('Resuming unfinished checks. Completed checks are preserved.');
    }catch(error){toast(error.message);button.disabled=false;}
  }
  if(button.id==='new-run-button')newRun();
  if(button.id==='clear-filters'){filters=emptyFilters();resultPage();}
  if(button.dataset.results)openResults(JSON.parse(button.dataset.results));
  if(button.dataset.signal)openResults({[button.dataset.signal]:'flagged'});
  if(button.dataset.tab){detailTab=button.dataset.tab;document.querySelectorAll('[data-tab]').forEach(tab=>{tab.classList.toggle('active',tab.dataset.tab===detailTab);tab.setAttribute('aria-selected',tab.dataset.tab===detailTab);});$('#case-evidence').innerHTML=detailContent();}
  if(button.dataset.evidence){const [key,index]=button.dataset.evidence.split(':');showEvidence(key,Number(index));}
});
document.addEventListener('change',async event=>{
  if(event.target.matches('.run-picker')){
    runId=event.target.value;
    filters=emptyFilters();
    await refreshRun();
    if(location.hash.startsWith('#case/'))location.hash='results';
    else await render();
  }
  if(event.target.dataset.filter){filters[event.target.dataset.filter]=event.target.value;renderTable();}
});
document.addEventListener('input',event=>{if(event.target.id==='search'){filters.search=event.target.value;renderTable();}});
$('#run-form').addEventListener('submit',async event=>{
  event.preventDefault();
  const body={mode:'openai',case_ids:corpus.slice(0,Number($('#batch-size').value)).map(c=>c.id)};
  $('#start-run').disabled=true;
  try {
    runId=(await api('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).id;
    $('#run-dialog').close();await refreshRun();location.hash='overview';await render();toast('Evaluation started. Results will appear as each review completes.');
  }catch(error){toast(error.message);$('#start-run').disabled=false;}
});
window.addEventListener('hashchange',()=>{render().catch(error=>toast(error.message));window.scrollTo(0,0);});
async function initialize(){
  try {
    [config,corpus]=await Promise.all([api('/api/config'),api('/api/cases')]);
    await refreshRun();await render();
    setInterval(async()=>{
      if(!runList.some(r=>r.status==='running') || busy)return;
      busy=true;
      try{
        await refreshRun();
        if(location.hash.startsWith('#case/') && currentCase && ['pending','running'].includes(currentCase.status)){
          const route=location.hash;
          const refreshed=await api(`/api/runs/${runId}/cases/${encodeURIComponent(currentCase.case.id)}`);
          if(location.hash===route && (refreshed.status!==currentCase.status || refreshed.error!==currentCase.error || JSON.stringify(refreshed.check_results)!==JSON.stringify(currentCase.check_results))){currentCase=refreshed;detailPage();}
        }else if(!location.hash.startsWith('#case/') && !$('#run-dialog').open){
          if(page==='overview')overview();
          else if(page==='results'){const focused=document.activeElement?.id;if(focused!=='search')resultPage();else renderTable();}
        }
      }
      catch(error){toast(error.message);}finally{busy=false;}
    },2500);
  }catch(error){$('#main').innerHTML=`<div class="empty"><h3>Reviewer unavailable</h3><p>${esc(error.message)}</p><p>Check that the local API is running, then reload.</p></div>`;}
}
initialize();
