'use strict';
(() => {
  let design = null, current = null, actionPending = false;
  const labels = {planned:'설계 준비',scan_pending:'셀 완료 · Tg 스캔 제출 필요',submitted:'계산 제출됨',running:'실행 중',queued:'대기',completed:'계산 완료',failed:'실패',blocked:'엔진 확인 필요',cancelled:'취소됨',interrupted:'중단됨',analyzing:'분석 중',analysis_failed:'분석 실패',observed:'반응값 수집됨',review_results:'반응값 검토',optimizing:'최적화 중',recommendation_ready:'추천 확인',confirmation_review:'확인 계산 비교',missing:'작업 없음',submission_failed:'제출 실패'};
  const metricLabels = {final_frame_density:'최종 구조 밀도',probe_accessible_fraction:'탐침 접근 가능 부피 비율',npt_density:'NPT 평균 밀도',bulk_modulus:'체적 탄성률',volume_expansion:'부피 팽창 계수',epoxide_conversion:'에폭시 전환율',tg_kelvin:'Tg (냉각 MD)',expansion_glass:'유리 쪽 팽창 계수',expansion_melt:'고무 쪽 팽창 계수'};
  const routeLabels = {gaff2_bulk:'GAFF2 벌크 · 자체 셀 빌더',pcff_bulk:'PCFF 벌크 · EMC',crosslink:'에폭시–아민 가교',tg_scan:'Tg 스캔 · 앞 흐름의 셀'};
  let capabilities = null;
  async function routeForm() {
    capabilities = capabilities || await api('/api/research/workflows/capabilities');
    const route = $('#flow-route').value, spec = capabilities.routes[route];
    $$('[data-route-group]').forEach(el => el.classList.toggle('hidden', el.dataset.routeGroup !== (route === 'crosslink' ? 'crosslink' : 'bulk')));
    $$('[data-route-only]').forEach(el => el.classList.toggle('hidden', el.dataset.routeOnly !== route));
    // Default each factor to the first unused setting whose allowed range holds the factor's range.
    const used = new Set(), fits = (f, key) => { const [lo, hi] = capabilities.fields[key].bounds; return f.low >= lo && f.high <= hi; };
    const choice = design ? design.factors.map(f => { const key = spec.fields.find(k => !used.has(k) && fits(f, k)) || spec.fields.find(k => !used.has(k)); used.add(key); return key; }) : [];
    if (design) $('#flow-bindings').innerHTML = design.factors.map((f, i) => `<label class="field"><span>${escape(f.name)} · ${number(f.low)}~${number(f.high)}</span><select data-binding="${escape(f.name)}">${spec.fields.map(key => `<option value="${key}" ${choice[i] === key ? 'selected' : ''}>${escape(capabilities.fields[key].label)} · ${capabilities.fields[key].bounds.map(number).join('~')}${capabilities.fields[key].integer ? ' · 정수' : ''}</option>`).join('')}</select></label>`).join('');
    $('#flow-metric').innerHTML = spec.responses.map(key => `<option value="${key}">${escape(metricLabels[key] || key)} · ${escape(capabilities.metrics[key])}</option>`).join('');
    $('#flow-response-options').innerHTML = spec.responses.map(key => `<label class="checkbox-field"><input type="checkbox" data-response value="${key}"> ${escape(metricLabels[key] || key)}</label>`).join('');
  }
  function templateFor(route) {
    const base = {name:'연결된 연구 흐름',route:'polymer_atomistic',protocol_scale:Number($('#flow-scale').value),seed:Number($('#flow-seed').value),temperature_kelvin:Number($('#flow-temperature').value)};
    if (route === 'crosslink') return {...base,atomistic_mode:'crosslink',resin_smiles:$('#flow-resin').value,hardener_smiles:$('#flow-hardener').value,resin_count:Number($('#flow-resin-count').value),hardener_count:Number($('#flow-hardener-count').value),cure_temperature_kelvin:Number($('#flow-cure-temperature').value),target_conversion:Number($('#flow-target-conversion').value),reaction_cutoff_angstrom:Number($('#flow-cutoff').value),cure_steps:Number($('#flow-cure-steps').value)};
    return {...base,atomistic_mode:'bulk',atomistic_forcefield:route==='pcff_bulk'?'pcff':'gaff2',...(route==='gaff2_bulk'?{cell_builder:'native',charge_method:$('#flow-charge').value}:{}),repeat_smiles:$('#flow-smiles').value,monomers:Number($('#flow-monomers').value),chains:Number($('#flow-chains').value)};
  }
  function routeSummary(record) {
    const route = record.route || 'gaff2_bulk', t = record.source_template || record.template;
    if (route === 'tg_scan') return `${routeLabels.tg_scan} · ${number(record.template.tg_high_kelvin)} → ${number(record.template.tg_low_kelvin)} K · 온도당 ${record.template.tg_stage_steps} 스텝 · 원래 흐름 ${escape(record.parent_workflow_id.slice(0, 8))} (${escape(routeLabels[record.source_route] || '')})`;
    if (route === 'crosslink') return `${routeLabels.crosslink} · ${escape(t.resin_smiles)} × ${t.resin_count} + ${escape(t.hardener_smiles)} × ${t.hardener_count} · 길이 배율 ${number(t.protocol_scale)}`;
    return `${routeLabels[route]} · ${escape(t.repeat_smiles)} · ${t.monomers} 반복단위 × ${t.chains} 사슬 · 길이 배율 ${number(t.protocol_scale)}`;
  }

  const collected = record => record.responses || [record.metric];
  const unitOf = (record, name) => (record.units || {})[name] || (name === record.metric ? record.unit : '');
  const valuesOf = (record, observation) => observation ? (observation.values || {[record.metric]: observation.value}) : {};
  let goalsFor = '';
  function goalRows(record) {
    const key = record.id + collected(record).join();
    if (key === goalsFor) return;
    goalsFor = key;
    $('#flow-response').innerHTML = collected(record).map(name => `<option value="${escape(name)}">${escape(metricLabels[name] || name)} · ${escape(unitOf(record, name))}</option>`).join('');
    $('#flow-goals').innerHTML = `<table class="research-table"><thead><tr><th>반응값</th><th>목표</th><th>모델</th><th>하한</th><th>목표값</th><th>상한</th></tr></thead><tbody>${collected(record).map(name => `<tr data-goal="${escape(name)}"><td>${escape(metricLabels[name] || name)}<small>${escape(unitOf(record, name))}</small></td><td><select data-key="goal"><option value="">사용 안 함</option><option value="maximize">최대화</option><option value="minimize">최소화</option><option value="target">목표값 맞추기</option></select></td><td><select data-key="model"><option value="quadratic">2차</option><option value="interaction">상호작용</option><option value="linear">주효과</option></select></td>${['lower','target','upper'].map(k => `<td><input type="number" step="any" data-key="${k}" aria-label="${escape(name)} ${k}"></td>`).join('')}</tr>`).join('')}</tbody></table>`;
  }
  function goals() {
    return $$('#flow-goals [data-goal]').filter(row => row.querySelector('[data-key=goal]').value).map(row => {
      const goal = {name: row.dataset.goal, goal: row.querySelector('[data-key=goal]').value, model: row.querySelector('[data-key=model]').value};
      for (const key of ['lower', 'target', 'upper']) { const value = row.querySelector(`[data-key=${key}]`).value; if (value !== '') goal[key] = Number(value); }
      return goal;
    });
  }
  const exclusions = () => { const numbers = $('#flow-excluded').value.trim().split(',').map(x => x.trim()).filter(Boolean), reason = $('#flow-exclusion-reason').value.trim(); return numbers.length && !reason ? null : Object.fromEntries(numbers.map(n => [n, reason])); };
  const number = value => Number.isFinite(value) ? escape(formatNumber(value)) : '—';
  const message = (text, error=false) => { $('#flow-message').innerHTML = `<p class="${error?'error-message':'research-footnote'}" role="status">${escape(text)}</p>`; };
  const selections = () => $$('#flow-rows input[data-run]:checked').map(el=>Number(el.dataset.run));
  async function historyList() {
    try {
      const result=await api('/api/research/workflows');
      $('#flow-history').innerHTML='<option value="">흐름 선택</option>'+result.records.map(r=>`<option value="${escape(r.id)}">${escape(timeLabel(r.created_at))} · ${escape(routeLabels[r.route||'gaff2_bulk'])} · ${escape(metricLabels[r.metric])} · ${escape(labels[r.state]||r.state)}</option>`).join('');
      if(current) $('#flow-history').value=current.id;
    } catch(error) { message(error.message,true); }
  }
  function render(record) {
    const selected=new Set(selections()); current=record;
    $('#flow-detail').classList.remove('hidden');
    $('#flow-summary').innerHTML=`<h3>2. 계산 실행 · 결과 검토</h3><p>${escape(labels[record.state]||record.state)} · 사용 예산 ${record.submitted}/${record.budget} · ${escape(metricLabels[record.metric])} (${escape(record.unit)})</p><p class="research-footnote">설계 ${record.rows.length}행 · ${routeSummary(record)}</p>${(record.source_template||record.template).protocol_scale<1?'<p class="research-tag amber">단축 MD · 평형·물성 정확도 미검증</p>':''}${record.missing_engines.length?`<p class="error-message">필요 엔진: ${escape(record.missing_engines.join(', '))}</p>`:''}${record.error?`<p class="error-message">${escape(record.error)}</p>`:''}<div class="research-actions"><a class="secondary-button" href="${escape(safeURL(record.download_url))}" download>연결 기록 JSON ↓</a>${reportButton('workflow', record.id)}</div>`;
    $('#flow-rows').innerHTML=`<table class="research-table"><thead><tr><th>선택</th><th>행</th>${record.factors.map(f=>`<th>${escape(f.name)}</th>`).join('')}<th>시드</th><th>상태</th>${collected(record).map(name=>`<th>${escape(metricLabels[name]||name)}<small>${escape(unitOf(record,name))}</small></th>`).join('')}<th>작업</th></tr></thead><tbody>${record.rows.map(row=>`<tr><td><input type="checkbox" aria-label="${row.run_order}행 선택" data-run="${row.run_order}" ${selected.has(row.run_order)?'checked':''}></td><td>${row.run_order}</td>${record.factors.map(f=>`<td>${number(row.factors[f.name])}</td>`).join('')}<td>${row.config.seed}</td><td>${escape(labels[['observed','analysis_failed','interrupted'].includes(row.state)?row.state:row.job_status]||labels[row.state]||row.state)}${row.error?`<br><span class="error-message">${escape(row.error)}</span>`:''}</td>${collected(record).map((name,i)=>`<td>${number(valuesOf(record,row.observation)[name])}${i===0&&row.observation?.analysis_id?`<br><a href="/api/research/analyses/${escape(row.observation.analysis_id)}/download" download>분석 JSON ↓</a>`:''}${i===0&&row.observation&&row.observation.equilibrated===false?'<br><span class="research-tag amber">NPT 미수렴</span>':''}</td>`).join('')}<td>${row.attempts.length?`<button class="text-button" data-flow-job="${escape(row.job_id)}">열기 ↗</button>`:'미실행'}</td></tr>`).join('')}</tbody></table>`;
    goalRows(record);
    $('#flow-tg').classList.toggle('hidden',(record.route||'gaff2_bulk')==='tg_scan');
    const result=record.optimizations.at(-1);
    const modelReview=result?.research_workflow;
    const diagnosticNote=result?.diagnostics?.unusual_rows ? `<p>예측 R² ${number(result.fit.predicted_r_squared)} · 적합 결여 p ${number(result.anova?.lack_of_fit?.p_value)} · 확인할 관측 ${result.diagnostics.unusual_rows.length}행</p>` : '';
    const reviewNote=modelReview ? `<div class="doe-next-step"><h3>${modelReview.stage==='model_review'?'모델 검토 필요':'독립 확인 계산 단계'}</h3>${modelReview.issues?.length?`<ul>${modelReview.issues.map(issue=>`<li>${escape(issue.message)}</li>`).join('')}</ul>`:''}${modelReview.next_steps?.length?`<ol>${modelReview.next_steps.map(step=>`<li>${escape(step)}</li>`).join('')}</ol>`:''}</div>` : '';
    const previous = record.confirmations.filter(c=>c.optimization_id===result?.id).at(-1);
    const retry = previous && ['failed','blocked','cancelled','interrupted','missing'].includes(previous.job_status);
    const desirable=result?.analysis==='doe_desirability';
    const predicted=!result?'':desirable?`<p>종합 만족도 D ${number(result.recommendation.composite_desirability)}</p>${Object.entries(result.recommendation.predicted).map(([name,val])=>`<p>${escape(metricLabels[name]||name)} 예측 ${number(val)} ${escape(unitOf(record,name))} · 95% 예측구간 ${result.recommendation.prediction_intervals[name].map(number).join(' ~ ')}</p>`).join('')}`:`<p>${escape(metricLabels[result.metric||record.metric]||'')} 예측값 ${number(result.recommendation.predicted_response)} ${escape(result.unit||record.unit)}</p>${result.recommendation.prediction_interval?`<p>개별 결과의 95% 예측구간: ${result.recommendation.prediction_interval.map(number).join(' ~ ')}</p>`:''}`;
    $('#flow-recommendation').innerHTML=result?`<div class="doe-recommendation"><h3>추천 조건 · 확인 계산 필요</h3>${Object.entries(result.recommendation.settings).map(([key,val])=>`<p>${escape(key)}: ${number(val)}</p>`).join('')}${predicted}<p class="research-footnote">시뮬레이션 기반 미검증 후보입니다.${Object.values(record.bindings).includes('build_density')?' 초기 패킹 밀도 최적화는 실제 소재 물성 개선을 보장하지 않습니다.':''}${result.recommendation.rounded?` 정수 설정은 반올림한 조건(${Object.entries(result.recommendation.rounded.settings).map(([k,v])=>`${escape(k)} ${number(v)}`).join(', ')})으로 확인 계산합니다.`:''}</p>${reviewNote}<details><summary>적합·제외 기록</summary>${desirable?result.responses.map(r=>`<p>${escape(metricLabels[r.name]||r.name)} · R² ${number(r.fit.r_squared)} · 예측 R² ${number(r.fit.predicted_r_squared)} · d ${number(r.desirability)}</p>`).join(''):`<p>R² ${number(result.fit.r_squared)} · RMSE ${number(result.fit.rmse)}</p>`}${diagnosticNote}<p>${escape(Object.entries(result.exclusions).map(([row,reason])=>`${row}행: ${reason}`).join(' / ')||'제외 없음')}</p></details><button class="secondary-button" id="flow-confirm" ${previous&&!retry?'disabled':''} data-retry="${retry?'true':'false'}">${retry?'실패한 확인 계산 재시도':'추천 조건 확인 · 별도 계산 1개 실행'}</button></div>`:'';
    pilotStatus(record);
    $('#flow-recommendation').innerHTML+=record.confirmations.map(c=>`<p>확인 계산 · ${escape(labels[c.final_status||c.job_status]||c.final_status||c.job_status||'제출됨')} · <button class="text-button" data-flow-job="${escape(c.job_id)}">${c.scan_job_id!==undefined?'셀 작업':'작업'} 열기 ↗</button>${c.scan_job_id?` · <button class="text-button" data-flow-job="${escape(c.scan_job_id)}">Tg 스캔 열기 ↗</button>`:''}${c.final_status==='scan_pending'?` <button class="secondary-button" data-confirm-analysis="${escape(c.job_id)}">셀 완료 · Tg 스캔 제출</button>`:(c.final_status||c.job_status)==='completed'?` <button class="secondary-button" data-confirm-analysis="${escape(c.scan_job_id||c.job_id)}">확인 결과 분석·비교</button>`:''}${c.observation?`<br>${Object.entries(c.observation.prediction_errors||{[c.observation.method||record.metric]:c.observation.prediction_error}).map(([name,error])=>`${escape(metricLabels[name]||name)} 관측 ${number(valuesOf(record,c.observation)[name])} ${escape(unitOf(record,name))} · 예측과 차이 ${number(error)}`).join('<br>')} · 미검증 시뮬레이션`:''}</p>`).join('');
  }
  const pilotModels={quadratic:'2차 반응표면',interaction:'주효과 + 상호작용',linear:'주효과'},pilotGoals={maximize:'최대화',minimize:'최소화',target:'목표값 맞추기'};
  const pilotLabels={running:'자동 실행 중',completed:'계획 완료',stopped:'멈춤',failed:'오류로 멈춤',cancelled:'취소됨',interrupted:'서버 재시작으로 멈춤'};
  function pilotStatus(record){
    const pilot=record.autopilot;
    if(!pilot){$('#flow-autopilot-status').innerHTML='';return;}
    const spent=record.submitted-(pilot.submitted_at_approval||0);
    $('#flow-autopilot-status').innerHTML=`<div class="doe-next-step"><h3>${escape(pilotLabels[pilot.state]||pilot.state)} <span class="research-tag ${pilot.state==='completed'?'':'amber'}">계산 ${spent}/${pilot.approved_budget} 사용</span></h3>${pilot.stopped_reason?`<p>${escape(pilot.stopped_reason)}</p>`:''}<ul>${(pilot.events||[]).slice(-8).map(e=>`<li>${escape(timeLabel(e.time))} · ${escape(e.message)}</li>`).join('')}</ul>${pilot.state==='running'?'<button class="secondary-button" type="button" id="pilot-stop">자동 실행 멈추기</button>':''}${pilot.report_id?reportLinks(pilot.report_id):''}<p class="research-footnote">승인 ${escape(timeLabel(pilot.approved_at))} · ${escape(pilot.approved_by||'')} · 계획 해시 ${escape((pilot.plan_sha256||'').slice(0,12))}</p></div>`;
  }
  const reportLinks=id=>`<div class="research-actions"><a class="secondary-button" href="${escape(safeURL(`/api/research/reports/${id}/view`))}" target="_blank" rel="noopener">자동 실행 보고서 보기 ↗</a></div>`;
  function pilotPlan(){
    const rows=selections();
    const mode=$('#pilot-mode').value==='desirability'?{goals:goals()}:{response:$('#flow-response').value||null};
    return {...(rows.length?{rows}:{}),...mode,max_attempts:Number($('#pilot-attempts').value),max_failed_rows:Number($('#pilot-failures').value),optimize:$('#pilot-optimize').checked,
      model:$('#flow-model').value,objective:$('#flow-objective').value,target:$('#flow-objective').value==='target'?Number($('#flow-target').value):null,
      confirm:$('#pilot-confirm').checked,report:$('#pilot-report').checked,upload_report:$('#pilot-upload').checked};
  }
  async function refresh() { if(current) render(await api(`/api/research/workflows/${current.id}`)); }
  async function act(path,body) {
    if(!current || actionPending)return;
    actionPending=true; message('실행 요청을 처리하고 있습니다. 다른 계산이 있으면 공통 큐에서 대기합니다.');
    try { render(await api(`/api/research/workflows/${current.id}/${path}`,{method:'POST',body:JSON.stringify(body||{})})); message('기록을 저장했습니다. 결과를 확인한 뒤 다음 단계를 실행하세요.'); await historyList(); }
    catch(error){message(error.message,true);await refresh().catch(()=>{});}
    finally{actionPending=false;}
  }
  document.addEventListener('click',async event=>{
    const connect=event.target.closest('[data-connect-design]');
    if(connect){
      try{
        design=await api(`/api/research/doe/${encodeURIComponent(connect.dataset.connectDesign)}`);
        await routeForm();
        $('#flow-create').classList.remove('hidden'); $('#flow-start-note').textContent=`선택 설계: ${design.total_runs}행 · 인자의 단위와 범위를 확인하세요.`;
        $('#bulk-flow-panel').scrollIntoView({block:'start'}); message('설정 저장은 계산을 시작하지 않습니다.');
      }catch(error){message(error.message,true);}
    }
    const job=event.target.closest('[data-flow-job]');if(job)openJob(job.dataset.flowJob);
    if(event.target.closest('#flow-confirm')&&current)act('confirm',{optimization_id:current.optimizations.at(-1).id,retry_failed:event.target.closest('#flow-confirm').dataset.retry==='true'});
    const confirmation=event.target.closest('[data-confirm-analysis]');if(confirmation)act('analyze-confirmation',{job_id:confirmation.dataset.confirmAnalysis});
  });
  $('#flow-create').addEventListener('submit',async event=>{
    event.preventDefault();if(!design||actionPending)return;actionPending=true;
    const bindings=Object.fromEntries($$('#flow-bindings [data-binding]').map(el=>[el.dataset.binding,el.value]));
    const template=templateFor($('#flow-route').value);
    try{render(await api('/api/research/workflows',{method:'POST',body:JSON.stringify({design_id:design.id,template,bindings,metric:$('#flow-metric').value,responses:$$('[data-response]:checked').map(el=>el.value).filter(v=>v!==$('#flow-metric').value),probe:Number($('#flow-probe').value),spacing:Number($('#flow-spacing').value),budget:Number($('#flow-budget').value)})}));$('#flow-create').classList.add('hidden');message('흐름을 저장했습니다. 실행할 행을 선택하고 설정을 확인하세요.');await historyList();}
    catch(error){message(error.message,true);}finally{actionPending=false;}
  });
  function preview(retry){
    if(!current)return;const rows=selections();if(!rows.length)return message('실행할 행을 선택하세요.',true);
    const area=$('#flow-submit-preview');area.classList.remove('hidden');
    area.innerHTML=`<h3>실행 전 확인</h3><p>선택한 행: ${rows.join(', ')} · ${retry?'실패한 작업만 새 ID로 재시도':'이미 제출한 행은 중복 실행하지 않음'}</p><p>최대 ${rows.length}개 계산 · 전체 예산 ${current.budget}, 사용 ${current.submitted}. 작업은 한 번에 1개씩 실행합니다.</p><button class="primary-button" id="flow-submit-confirm">위 설정을 확인하고 계산 실행</button>`;
    $('#flow-submit-confirm').onclick=()=>{area.classList.add('hidden');act('submit',{rows,retry_failed:retry});};
  }
  $('#pilot-preview').onclick=async()=>{
    if(!current)return;const plan=pilotPlan();
    try{
      const result=await api(`/api/research/workflows/${current.id}/autopilot/preview`,{method:'POST',body:JSON.stringify(plan)});
      $('#pilot-plan').innerHTML=`<div class="doe-recommendation"><h3>승인할 계획</h3><p>계산할 행 ${result.rows_to_run.length}개(${escape(result.rows_to_run.join(', ')||'없음')}) · 행마다 최대 ${result.plan.max_attempts}회 · 허용 실패 ${result.plan.max_failed_rows}행</p>${result.excluded_by_plan.length?`<p>계획 밖이라 최적화에서 제외할 행: ${escape(result.excluded_by_plan.join(', '))}</p>`:''}<p>${!result.plan.optimize?'최적화 없음':result.plan.goals?`만족도 목표 ${result.plan.goals.map(g=>escape(metricLabels[g.name]||g.name)+' '+escape(pilotGoals[g.goal])).join(', ')}${result.plan.confirm?' · 확인 계산 1개':''}`:`${escape(metricLabels[result.plan.response||current.metric]||'')} · ${escape(pilotModels[result.plan.model])} · ${escape(pilotGoals[result.plan.objective])}${result.plan.confirm?' · 확인 계산 1개':''}`}${result.plan.report?' · 보고서':''}${result.plan.upload_report?' · Drive 업로드':''}</p><p><strong>최대 ${result.worst_case_runs}개 계산</strong> · 남은 예산 ${result.remaining_budget}개</p>${result.missing_engines.length?`<p class="error-message">필요 엔진: ${escape(result.missing_engines.join(', '))}</p>`:''}<button class="primary-button" type="button" id="pilot-approve" data-budget="${result.worst_case_runs}">이 계획과 최대 ${result.worst_case_runs}개 계산을 승인하고 실행</button></div>`;
      $('#pilot-approve').onclick=()=>{$('#pilot-plan').innerHTML='';act('autopilot',{plan,approved_budget:result.worst_case_runs});};
    }catch(error){$('#pilot-plan').innerHTML=`<p class="error-message">${escape(error.message)}</p>`;}
  };
  document.addEventListener('click',event=>{if(event.target.closest('#pilot-stop')&&current)act('autopilot/stop',{});});
  $('#flow-route').onchange=()=>routeForm().catch(e=>message(e.message,true));
  $('#flow-tg-create').onclick=async()=>{
    if(!current||actionPending)return;actionPending=true;
    try{render(await api(`/api/research/workflows/${current.id}/tg-scan`,{method:'POST',body:JSON.stringify({tg_high_kelvin:Number($('#flow-tg-high').value),tg_low_kelvin:Number($('#flow-tg-low').value),tg_step_kelvin:Number($('#flow-tg-step').value),tg_stage_steps:Number($('#flow-tg-steps').value),budget:Number($('#flow-tg-budget').value)})}));$('#flow-tg').open=false;message('Tg 스캔 흐름을 저장했습니다. 계산은 아직 시작하지 않았습니다.');await historyList();}
    catch(error){message(error.message,true);}finally{actionPending=false;}
  };
  $('#flow-preview').onclick=()=>preview(false);$('#flow-retry').onclick=()=>preview(true);
  $('#flow-analyze').onclick=()=>{const rows=selections();rows.length?act('analyze',{rows}):message('분석할 완료 행을 선택하세요.',true);};
  $('#flow-refresh').onclick=()=>refresh().catch(e=>message(e.message,true));
  $('#flow-cancel').onclick=async()=>{if(!current)return;try{render(await api(`/api/research/workflows/${current.id}/cancel`,{method:'POST'}));message('이 흐름의 실행 취소를 요청했습니다.');}catch(e){message(e.message,true);}};
  $('#flow-attach').onclick=()=>act('attach',{run_order:Number($('#flow-attach-row').value),job_id:$('#flow-attach-job').value.trim()});
  $('#flow-history').onchange=async event=>{if(!event.target.value)return;try{render(await api(`/api/research/workflows/${event.target.value}`));$('#flow-create').classList.add('hidden');$('#flow-submit-preview').classList.add('hidden');}catch(e){message(e.message,true);}};
  $('#flow-objective').onchange=()=>{$('#flow-target-field').classList.toggle('hidden',$('#flow-objective').value!=='target');$('#flow-target').required=$('#flow-objective').value==='target';};
  $('#flow-optimize').onsubmit=event=>{event.preventDefault();const excluded=exclusions();if(!excluded)return message('제외 사유를 입력하세요.',true);act('optimize',{model:$('#flow-model').value,objective:$('#flow-objective').value,target:$('#flow-objective').value==='target'?Number($('#flow-target').value):null,exclusions:excluded,response:$('#flow-response').value||null});};
  $('#flow-desirability').onsubmit=event=>{event.preventDefault();const excluded=exclusions(),chosen=goals();if(!excluded)return message('제외 사유를 입력하세요.',true);if(!chosen.length)return message('목표를 정할 반응값을 하나 이상 고르세요.',true);act('optimize-desirability',{goals:chosen,exclusions:excluded});};
  async function execution(){try{const status=await api('/api/execution'),tasks=status.tasks.filter(t=>['queued','running'].includes(t.status)),lanes=Object.entries(status.lanes||{}).map(([key,lane])=>`${lane.label} ${tasks.filter(t=>t.lane===key&&t.status==='running').length}/${lane.limit}`).join(' · ');$('#execution-status').textContent=`실행 큐 · ${status.engine==='rust'?'Rust':'Python 호환'} · ${lanes} · 대기 ${tasks.filter(t=>t.status==='queued').length+(status.pending_jobs||0)}`;}catch{}}
  document.addEventListener('polymer:view',event=>{if(event.detail==='doe'){historyList();execution();}});
  setInterval(()=>{if(state.view==='doe'){execution();if(current&&!actionPending)refresh().catch(()=>{});}},5000);
  if(state.view==='doe'){historyList();execution();}
})();
