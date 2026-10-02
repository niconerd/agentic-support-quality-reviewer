const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const elements = new Map();
const context = vm.createContext({
  localStorage: {getItem: () => 'saved-run'},
  location: {hash:'#overview'},
  document: {addEventListener(){}, querySelector(selector){
    if (!elements.has(selector)) elements.set(selector, {innerHTML:'', addEventListener(){}});
    return elements.get(selector);
  }},
  window: {addEventListener(){}},
});
const source = fs.readFileSync(path.join(__dirname,'../reviewer/static/app.js'),'utf8');
vm.runInContext(source.replace(/initialize\(\);\s*$/, ''),context);
const run = code => vm.runInContext(code,context);
run(`
config={passing:['RESOLVED','COMPLIANT','CORRECT','APPROPRIATE'],framework:[
 {key:'resolution',name:'Resolution Correctness',short:'Resolution',outcomes:{RESOLVED:'',UNRESOLVED:''}},
 {key:'compliance',name:'Policy Compliance',short:'Policy',outcomes:{COMPLIANT:'','NON-COMPLIANT':'','CRITICAL NON-COMPLIANCE':''}},
 {key:'factuality',name:'Factual Correctness',short:'Factuality',outcomes:{CORRECT:'',INACCURATE:'','CONTRADICTED BY SYSTEM EVIDENCE':''}},
 {key:'communication',name:'Communication Appropriateness',short:'Communication',outcomes:{APPROPRIATE:'','NEEDS IMPROVEMENT':''}}
]};
const evaluated=(id,changes={})=>({case_id:id,scenario:'Returns',tags:['Returns'],objective:'Return order',status:'completed',evaluation:Object.fromEntries(Object.entries({resolution:'RESOLVED',compliance:'COMPLIANT',factuality:'CORRECT',communication:'APPROPRIATE',...changes}).map(([key,result])=>[key,{result}]))});
const samples=[evaluated('good'),evaluated('flagged',{resolution:'UNRESOLVED',factuality:'INACCURATE'}),evaluated('critical',{compliance:'CRITICAL NON-COMPLIANCE',factuality:'CONTRADICTED BY SYSTEM EVIDENCE'}),{case_id:'pending',scenario:'Returns',tags:[],objective:'',status:'pending',evaluation:null},{case_id:'failed',scenario:'Returns',tags:[],objective:'',status:'failed',evaluation:null}];
activeRun={id:'saved-run',total_cases:5,counts:{completed:3,pending:1,running:0,failed:1},results:samples,config:{},status:'partial'};
`);
const ids = selection => {
  run(`filters={...emptyFilters(),...${JSON.stringify(selection)}}`);
  return Array.from(run('rows().filter(matchesFilters).map(r=>r.case_id)'));
};
assert.deepEqual(ids({scope:'reviewed'}),['good','flagged','critical']);
assert.deepEqual(ids({scope:'attention'}),['flagged','critical']);
assert.deepEqual(ids({factuality:'flagged'}),['flagged','critical']);
assert.deepEqual(ids({factuality:'CORRECT'}),['good']);
assert.deepEqual(ids({factuality:'INACCURATE'}),['flagged']);
assert.deepEqual(ids({compliance:'CRITICAL NON-COMPLIANCE'}),['critical']);
assert.deepEqual(ids({communication:'NEEDS IMPROVEMENT'}),[]);
assert.deepEqual(ids({scope:'attention',factuality:'INACCURATE',search:'flagged'}),['flagged']);
run("openResults({factuality:'CORRECT'})");
assert.equal(run('filters.scope'),'');
assert.equal(run('filters.search'),'');
assert.equal(run('runId'),'saved-run');
assert.equal(context.location.hash,'results');
const chart = run('attentionChart(rows().filter(r=>r.evaluation))');
assert.match(chart,/2 \/ 3 · 66.7%/);
assert.match(chart,/0 \/ 3 · 0%/);
assert.equal((chart.match(/class="attention-row"/g)||[]).length,4);
assert.doesNotMatch(run('attentionChart([])'),/NaN|Infinity/);
assert.match(run('attentionChart([])'),/Complete reviews/);
run('renderTable()');
assert.match(elements.get('#results-table').innerHTML,/1 of 5 conversations/);
assert.doesNotMatch(elements.get('#results-table').innerHTML,/href="#case\/flagged"/);
console.log('Dashboard checks passed: exact outcomes, combined filters, reviewed denominator, empty states, and navigation reset.');
