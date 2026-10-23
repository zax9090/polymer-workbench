'use strict';
(() => {
  let design = null, current = null, actionPending = false;
  const labels = {planned:'설계 준비',submitted:'계산 제출됨',running:'실행 중',queued:'대기',completed:'계산 완료',failed:'실패',blocked:'엔진 확인 필요',cancelled:'취소됨',interrupted:'중단됨',analyzing:'분석 중',analysis_failed:'분석 실패',observed:'반응값 수집됨',review_results:'반응값 검토',optimizing:'최적화 중',recommendation_ready:'추천 확인',confirmation_review:'확인 계산 비교',missing:'작업 없음',submission_failed:'제출 실패'};
  const fieldLabels = {temperature_kelvin:'온도 · K', build_density:'초기 패킹 밀도 · g/cm³'};
  const metricLabels = {final_frame_density:'최종 구조 밀도',probe_accessible_fraction:'탐침 접근 가능 부피 비율'};
  const number = value => Number.isFinite(value) ? escape(formatNumber(value)) : '—';
  const message = (text, error=false) => { $('#flow-message').innerHTML = `<p class="${error?'error-message':'research-footnote'}" role="status">${escape(text)}</p>`; };
  const selections = () => $$('#flow-rows input[data-run]:checked').map(el=>Number(el.dataset.run));
  async function historyList() {
    try {
      const result=await api('/api/research/workflows');
      $('#flow-history').innerHTML='<option value="">흐름 선택</option>'+result.records.map(r=>`<option value="${escape(r.id)}">${escape(timeLabel(r.created_at))} · ${escape(metricLabels[r.metric])} · ${escape(labels[r.state]||r.state)}</option>`).join('');
      if(current) $('#flow-history').value=current.id;
    } catch(error) { message(error.message,true); }
  }
  function render(record) {
    const selected=new Set(selections()); current=record;
    $('#flow-detail').classList.remove('hidden');
    $('#flow-summary').innerHTML=`<h3>2. 계산 실행 · 결과 검토</h3><p>${escape(labels[record.state]||record.state)} · 사용 예산 ${record.submitted}/${record.budget} · ${escape(metricLabels[record.metric])} (${escape(record.unit)})</p><p class="research-footnote">설계 ${record.rows.length}행 · ${escape(record.template.repeat_smiles)} · ${record.template.monomers} 반복단위 × ${record.template.chains} 사슬 · GAFF2 · 길이 배율 ${number(record.template.protocol_scale)}</p>${record.template.protocol_scale<1?'<p class="research-tag amber">단축 MD · 평형·물성 정확도 미검증</p>':''}${record.missing_engines.length?`<p class="error-message">필요 엔진: ${escape(record.missing_engines.join(', '))}</p>`:''}${record.error?`<p class="error-message">${escape(record.error)}</p>`:''}<a href="${escape(safeURL(record.download_url))}" download>연결 기록 JSON ↓</a>`;
    $('#flow-rows').innerHTML=`<table class="research-table"><thead><tr><th>선택</th><th>행</th>${record.factors.map(f=>`<th>${escape(f.name)}</th>`).join('')}<th>시드</th><th>상태</th><th>반응값</th><th>작업</th></tr></thead><tbody>${record.rows.map(row=>`<tr><td><input type="checkbox" aria-label="${row.run_order}행 선택" data-run="${row.run_order}" ${selected.has(row.run_order)?'checked':''}></td><td>${row.run_order}</td>${record.factors.map(f=>`<td>${number(row.factors[f.name])}</td>`).join('')}<td>${row.config.seed}</td><td>${escape(labels[['observed','analysis_failed','interrupted'].includes(row.state)?row.state:row.job_status]||labels[row.state]||row.state)}${row.error?`<br><span class="error-message">${escape(row.error)}</span>`:''}</td><td>${number(row.observation?.value)}${row.observation?`<br><a href="/api/research/analyses/${escape(row.observation.analysis_id)}/download" download>분석 JSON ↓</a>`:''}</td><td>${row.attempts.length?`<button class="text-button" data-flow-job="${escape(row.job_id)}">열기 ↗</button>`:'미실행'}</td></tr>`).join('')}</tbody></table>`;
    const result=record.optimizations.at(-1);
    const modelReview=result?.research_workflow;
    const diagnosticNote=result?.diagnostics?.unusual_rows ? `<p>예측 R² ${number(result.fit.predicted_r_squared)} · 적합 결여 p ${number(result.anova?.lack_of_fit?.p_value)} · 확인할 관측 ${result.diagnostics.unusual_rows.length}행</p>` : '';
    const reviewNote=modelReview ? `<div class="doe-next-step"><h3>${modelReview.stage==='model_review'?'모델 검토 필요':'독립 확인 계산 단계'}</h3>${modelReview.issues?.length?`<ul>${modelReview.issues.map(issue=>`<li>${escape(issue.message)}</li>`).join('')}</ul>`:''}${modelReview.next_steps?.length?`<ol>${modelReview.next_steps.map(step=>`<li>${escape(step)}</li>`).join('')}</ol>`:''}</div>` : '';
    const previous = record.confirmations.filter(c=>c.optimization_id===result?.id).at(-1);
    const retry = previous && ['failed','blocked','cancelled','interrupted','missing'].includes(previous.job_status);
    $('#flow-recommendation').innerHTML=result?`<div class="doe-recommendation"><h3>추천 조건 · 확인 계산 필요</h3>${Object.entries(result.recommendation.settings).map(([key,val])=>`<p>${escape(key)}: ${number(val)}</p>`).join('')}<p>예측값 ${number(result.recommendation.predicted_response)} ${escape(record.unit)}</p>${result.recommendation.prediction_interval?`<p>개별 결과의 95% 예측구간: ${result.recommendation.prediction_interval.map(number).join(' ~ ')}</p>`:''}<p class="research-footnote">시뮬레이션 기반 미검증 후보입니다. 초기 패킹 밀도 최적화는 실제 소재 물성 개선을 보장하지 않습니다.</p>${reviewNote}<details><summary>적합·제외 기록</summary><p>R² ${number(result.fit.r_squared)} · RMSE ${number(result.fit.rmse)}</p>${diagnosticNote}<p>${escape(Object.entries(result.exclusions).map(([row,reason])=>`${row}행: ${reason}`).join(' / ')||'제외 없음')}</p></details><button class="secondary-button" id="flow-confirm" ${previous&&!retry?'disabled':''} data-retry="${retry?'true':'false'}">${retry?'실패한 확인 계산 재시도':'추천 조건 확인 · 별도 계산 1개 실행'}</button></div>`:'';
    $('#flow-recommendation').innerHTML+=record.confirmations.map(c=>`<p>확인 계산 · ${escape(labels[c.job_status]||c.job_status||'제출됨')} · <button class="text-button" data-flow-job="${escape(c.job_id)}">작업 열기 ↗</button>${c.job_status==='completed'?` <button class="secondary-button" data-confirm-analysis="${escape(c.job_id)}">확인 결과 분석·비교</button>`:''}${c.observation?`<br>관측값 ${number(c.observation.value)} ${escape(c.observation.unit)} · 예측과 차이 ${number(c.observation.prediction_error)} · 미검증 시뮬레이션`:''}</p>`).join('');
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
        if(design.factors.length!==2)throw new Error('벌크 연결은 온도와 초기 패킹 밀도, 두 인자 설계부터 지원합니다.');
        $('#flow-bindings').innerHTML=design.factors.map((f,i)=>`<label class="field"><span>${escape(f.name)} · ${number(f.low)}~${number(f.high)}</span><select data-binding="${escape(f.name)}">${Object.entries(fieldLabels).map(([key,label],j)=>`<option value="${key}" ${i===j?'selected':''}>${label}</option>`).join('')}</select></label>`).join('');
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
    const template={name:'연결된 벌크 연구',route:'polymer_atomistic',atomistic_mode:'bulk',atomistic_forcefield:'gaff2',cell_builder:'native',repeat_smiles:$('#flow-smiles').value,monomers:Number($('#flow-monomers').value),chains:Number($('#flow-chains').value),protocol_scale:Number($('#flow-scale').value),charge_method:$('#flow-charge').value,seed:Number($('#flow-seed').value)};
    try{render(await api('/api/research/workflows',{method:'POST',body:JSON.stringify({design_id:design.id,template,bindings,metric:$('#flow-metric').value,probe:Number($('#flow-probe').value),spacing:Number($('#flow-spacing').value),budget:Number($('#flow-budget').value)})}));$('#flow-create').classList.add('hidden');message('흐름을 저장했습니다. 실행할 행을 선택하고 설정을 확인하세요.');await historyList();}
    catch(error){message(error.message,true);}finally{actionPending=false;}
  });
  function preview(retry){
    if(!current)return;const rows=selections();if(!rows.length)return message('실행할 행을 선택하세요.',true);
    const area=$('#flow-submit-preview');area.classList.remove('hidden');
    area.innerHTML=`<h3>실행 전 확인</h3><p>선택한 행: ${rows.join(', ')} · ${retry?'실패한 작업만 새 ID로 재시도':'이미 제출한 행은 중복 실행하지 않음'}</p><p>최대 ${rows.length}개 계산 · 전체 예산 ${current.budget}, 사용 ${current.submitted}. 작업은 한 번에 1개씩 실행합니다.</p><button class="primary-button" id="flow-submit-confirm">위 설정을 확인하고 계산 실행</button>`;
    $('#flow-submit-confirm').onclick=()=>{area.classList.add('hidden');act('submit',{rows,retry_failed:retry});};
  }
  $('#flow-preview').onclick=()=>preview(false);$('#flow-retry').onclick=()=>preview(true);
  $('#flow-analyze').onclick=()=>{const rows=selections();rows.length?act('analyze',{rows}):message('분석할 완료 행을 선택하세요.',true);};
  $('#flow-refresh').onclick=()=>refresh().catch(e=>message(e.message,true));
  $('#flow-cancel').onclick=async()=>{if(!current)return;try{render(await api(`/api/research/workflows/${current.id}/cancel`,{method:'POST'}));message('이 흐름의 실행 취소를 요청했습니다.');}catch(e){message(e.message,true);}};
  $('#flow-attach').onclick=()=>act('attach',{run_order:Number($('#flow-attach-row').value),job_id:$('#flow-attach-job').value.trim()});
  $('#flow-history').onchange=async event=>{if(!event.target.value)return;try{render(await api(`/api/research/workflows/${event.target.value}`));$('#flow-create').classList.add('hidden');$('#flow-submit-preview').classList.add('hidden');}catch(e){message(e.message,true);}};
  $('#flow-objective').onchange=()=>{$('#flow-target-field').classList.toggle('hidden',$('#flow-objective').value!=='target');$('#flow-target').required=$('#flow-objective').value==='target';};
  $('#flow-optimize').onsubmit=event=>{event.preventDefault();const numbers=$('#flow-excluded').value.trim().split(',').map(x=>x.trim()).filter(Boolean),reason=$('#flow-exclusion-reason').value.trim();if(numbers.length&&!reason)return message('제외 사유를 입력하세요.',true);act('optimize',{model:$('#flow-model').value,objective:$('#flow-objective').value,target:$('#flow-objective').value==='target'?Number($('#flow-target').value):null,exclusions:Object.fromEntries(numbers.map(n=>[n,reason]))});};
  async function execution(){try{const status=await api('/api/execution'),tasks=status.tasks.filter(t=>['queued','running'].includes(t.status)),lanes=Object.entries(status.lanes||{}).map(([key,lane])=>`${lane.label} ${tasks.filter(t=>t.lane===key&&t.status==='running').length}/${lane.limit}`).join(' · ');$('#execution-status').textContent=`실행 큐 · ${status.engine==='rust'?'Rust':'Python 호환'} · ${lanes} · 대기 ${tasks.filter(t=>t.status==='queued').length+(status.pending_jobs||0)}`;}catch{}}
  document.addEventListener('polymer:view',event=>{if(event.detail==='doe'){historyList();execution();}});
  setInterval(()=>{if(state.view==='doe'){execution();if(current&&!actionPending)refresh().catch(()=>{});}},5000);
  if(state.view==='doe'){historyList();execution();}
})();
