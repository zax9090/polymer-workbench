'use strict';
(() => {
  const base = '/api/research/density-studies';
  let current = null, pending = false;
  const statusNames = {completed:'계산 완료',queued:'대기',running:'실행 중',failed:'실패',blocked:'엔진·입력 확인',cancelled:'취소',interrupted:'중단',prepared:'입력 준비',missing:'작업 없음'};
  const fmt = value => Number.isFinite(value) ? escape(formatNumber(value)) : '—';
  const interval = value => Array.isArray(value) ? value.map(fmt).join(' ~ ') : '—';
  const message = (text, error=false) => { $('#density-message').innerHTML = `<p class="${error?'error-message':'research-footnote'}" role="status">${escape(text)}</p>`; };
  const optionalNumber = id => {
    const text = $(id).value.trim();
    if (!text) return null;
    const value = Number(text);
    if (!Number.isFinite(value) || value <= 0) throw new Error('허용 기준과 표준 불확실성에는 0보다 큰 유한한 숫자를 입력하세요.');
    return value;
  };
  async function action(work) {
    if (pending) return;
    pending = true;
    const buttons = $$('button', $('#density-study-panel'));
    buttons.forEach(button => { button.disabled = true; });
    $('#density-study-panel').setAttribute('aria-busy','true');
    try { await work(); }
    catch (error) { message(error.message,true); }
    finally {
      buttons.forEach(button => { button.disabled = false; });
      $('#density-study-panel').removeAttribute('aria-busy');
      pending = false;
    }
  }
  async function history() {
    const result = await api(base);
    $('#density-history').innerHTML = '<option value="">검토 선택</option>' + (result.records || []).map(item => `<option value="${escape(item.id)}">${escape(item.name || item.id.slice(0,8))} · ${escape(timeLabel(item.created_at))}</option>`).join('');
    if (current) $('#density-history').value = current.id;
  }
  function assessmentHTML(study, assessment) {
    if (!assessment) return '<p class="research-footnote">기존 작업을 연결하고 근거 평가를 눌러 실행·수렴·반복·대조 상태를 구분하세요.</p>';
    const stats = assessment.statistics, records = assessment.records || [];
    let html = window.qualityHTML(assessment.quality);
    if (stats) html += `<div class="research-features density-statistics"><div class="research-feature"><span>기술 통계에 포함된 값</span><strong>${fmt(stats.n)}개</strong><small>조건·출처 일치 기준</small></div><div class="research-feature"><span>반복 평균 · g/cm³</span><strong>${fmt(stats.mean)}</strong></div><div class="research-feature"><span>표본 표준편차 · g/cm³</span><strong>${fmt(stats.sample_sd)}</strong></div><div class="research-feature"><span>상대 표준편차</span><strong>${Number.isFinite(stats.relative_sd)?`${fmt(stats.relative_sd*100)}%`:'—'}</strong></div></div><p class="research-footnote">평균의 95% t 구간: ${interval(stats.confidence_interval)} g/cm³ · 독립·정규 반복 가정 아래의 구간입니다. 같은 조건의 미수렴 값도 기술 통계에 포함될 수 있으므로 수렴 판정을 따로 확인하세요. 전체 방법 오차를 포함한 구간은 아닙니다.</p>`;
    const comparison = stats?.reference_comparison;
    if (comparison) html += `<div class="research-kv"><span>참조값과 차이 · g/cm³</span><strong>${fmt(comparison.value_difference)}</strong></div><p class="research-footnote">합성 표준 불확실성 ${fmt(comparison.combined_standard_uncertainty)} g/cm³ · 미기록 불확실성을 0으로 가정하지 않습니다. 실제 대조 가능 여부는 위 근거 상태를 따릅니다.</p>`;
    html += `<details class="density-records" open><summary>연결된 계산과 포함·제외 근거 (${records.length})</summary><div class="table-scroll"><table class="research-table"><thead><tr><th>작업</th><th>seed</th><th>실행</th><th>NPT 밀도 · g/cm³</th><th>수렴</th><th>통계 포함</th><th>확인 사항</th></tr></thead><tbody>${records.map(record=>`<tr><td><button type="button" class="text-button mono" data-density-job="${escape(record.job_id)}">${escape(record.job_id.slice(0,8))} ↗</button></td><td>${fmt(record.seed)}</td><td>${escape(statusNames[record.status] || record.status)}</td><td>${fmt(record.density)}</td><td><span class="research-tag ${record.equilibrated===true?'':'amber'}">${record.equilibrated===true?'기록 기준 충족':record.equilibrated===false?'미수렴':'미기록'}</span></td><td>${record.eligible?'포함':'제외'}</td><td>${(record.issues || []).map(issue=>escape(issue.message || issue)).join('<br>') || '—'}</td></tr>`).join('')}</tbody></table></div></details>`;
    const reference = assessment.reference;
    if (reference?.measurement_id) {
      const row = reference.record || {};
      html += `<details class="density-reference-summary"><summary>연결한 참조 · ${reference.eligible?'대조 조건 일치':'추가 확인 필요'}</summary><p>값 ${fmt(row.value)} ${escape(row.unit || '')} · ${escape({experimental:'실험',literature:'문헌'}[row.origin] || row.origin || '출처 종류 미기록')} · ${row.verified===true?'검토된 참조':'참조 미검토'}</p><p class="research-footnote">${escape(row.method || '')} · ${escape(row.source || '')}</p>${reference.issues?.length?`<ul>${reference.issues.map(issue=>`<li>${escape(issue.message || issue)}</li>`).join('')}</ul>`:''}</details>`;
    }
    const aid = assessment.id;
    html += `<div class="research-actions"><a class="secondary-button" href="${escape(safeURL(`${base}/${study.id}/assessments/${aid}/download`))}" download>이번 평가 JSON ↓</a>${typeof reportButton==='function'?reportButton('density_study',study.id):''}</div>`;
    if (assessment.warnings?.length) html += `<details class="structure-warnings"><summary>계산 주의사항 (${assessment.warnings.length})</summary><ul>${assessment.warnings.map(warning=>`<li>${escape(warning)}</li>`).join('')}</ul></details>`;
    return html;
  }
  function render(study) {
    current = study;
    $('#density-detail').classList.remove('hidden');
    const cfg=study.template || {}, conditions=study.conditions || {};
    const jobs=study.job_ids || [];
    $('#density-summary').innerHTML = `<h3>${escape(study.name || '대표 밀도 검토')} <span class="research-tag amber">검토 전 시뮬레이션</span></h3><p class="research-footnote">${escape(cfg.repeat_smiles || '')} · ${fmt(conditions.temperature_kelvin ?? cfg.temperature_kelvin)} K · ${fmt(conditions.pressure_atm)} atm · ${fmt(cfg.monomers)} 반복단위 × ${fmt(cfg.chains)} 사슬 · 길이 배율 ${fmt(cfg.protocol_scale)}</p><div class="research-kv"><span>연결된 작업 / 계획 상한</span><strong>${fmt(jobs.length)} / ${fmt(study.max_replicates)}</strong></div><p class="research-footnote">최소 ${fmt(study.minimum_replicates)}회는 계획 기준이며 독립성·재현성의 증명은 아닙니다. 다른 조건의 DOE 행과 마지막 구조 밀도를 NPT 반복값으로 섞지 않습니다.</p><p class="research-footnote">정한 허용 기준 · 상대 표준편차 ${Number.isFinite(study.max_relative_sd)?`${fmt(study.max_relative_sd*100)}% 이하`:'미지정'} · 참조와 절대 차이 ${Number.isFinite(study.absolute_tolerance_g_cm3)?`${fmt(study.absolute_tolerance_g_cm3)} g/cm³ 이하`:'미지정'}</p>${study.download_url?`<a class="text-button" href="${escape(safeURL(study.download_url))}" download>검토 계획 JSON ↓</a>`:''}`;
    $('#density-assessment').innerHTML = assessmentHTML(study,study.latest_assessment);
    $('#density-history').value = study.id;
  }
  async function refresh() {
    if (!current) return;
    render(await api(`${base}/${current.id}`));
  }
  $('#density-create').addEventListener('submit',event=>{
    event.preventDefault();
    action(async()=>{
      const id=$('#density-source-job').value.trim();
      if (!/^[0-9a-f]{32}$/.test(id)) throw new Error('조건을 가져올 작업 ID 32자리를 입력하세요.');
      const settings={name:$('#density-name').value.trim() || '대표 밀도 검토',minimum_replicates:Number($('#density-minimum').value),max_replicates:Number($('#density-maximum').value),max_relative_sd:optionalNumber('#density-max-rsd'),absolute_tolerance_g_cm3:optionalNumber('#density-tolerance')};
      if (settings.max_replicates<settings.minimum_replicates) throw new Error('계획 상한은 최소 반복 수 이상이어야 합니다.');
      const source=await api(`/api/jobs/${id}`), cfg=source.config;
      if (cfg.route!=='polymer_atomistic' || cfg.atomistic_mode!=='bulk' || cfg.atomistic_forcefield!=='gaff2' || cfg.cell_builder!=='native') throw new Error('GAFF2 자체 벌크 작업의 설정을 선택하세요.');
      render(await api(base,{method:'POST',body:JSON.stringify({...settings,template:cfg})}));
      if (!$('#density-job-ids').value.trim()) $('#density-job-ids').value=id;
      message('검토 계획을 저장했습니다. 새 계산은 실행하지 않았습니다. 같은 조건의 기존 작업을 연결하세요.');
      await history();
    });
  });
  $('#density-attach').addEventListener('submit',event=>{
    event.preventDefault();action(async()=>{
      if (!current) throw new Error('검토 계획을 먼저 선택하거나 만드세요.');
      const ids=$('#density-job-ids').value.trim().split(/[\s,]+/).filter(Boolean);
      if (!ids.length || ids.some(id=>!/^[0-9a-f]{32}$/.test(id))) throw new Error('연결할 작업 ID를 쉼표나 줄바꿈으로 구분해 입력하세요.');
      render(await api(`${base}/${current.id}/attach`,{method:'POST',body:JSON.stringify({job_ids:ids})}));
      message('기존 작업을 연결했습니다. 근거 평가를 눌러 현재 원본과 상태를 확인하세요.');
    });
  });
  $('#density-reference').addEventListener('submit',event=>{
    event.preventDefault();action(async()=>{
      if (!current) throw new Error('검토 계획을 먼저 선택하거나 만드세요.');
      const measurement=$('#density-reference-id').value.trim();
      if (!/^[0-9a-f]{32}$/.test(measurement)) throw new Error('등록된 실험·문헌 측정값 ID를 입력하세요.');
      render(await api(`${base}/${current.id}/reference`,{method:'POST',body:JSON.stringify({measurement_id:measurement,standard_uncertainty:optionalNumber('#density-reference-u')})}));
      message('등록된 참조를 연결했습니다. 측정값의 검토 상태는 바꾸지 않았습니다.');
    });
  });
  $('#density-assess').addEventListener('click',()=>action(async()=>{
    if (!current) throw new Error('검토 계획을 먼저 선택하거나 만드세요.');
    await api(`${base}/${current.id}/assess`,{method:'POST'});
    await refresh();message('원본을 다시 확인해 이번 평가를 저장했습니다. 과학적 검증을 자동 승인하지 않습니다.');
  }));
  $('#density-refresh').addEventListener('click',()=>action(async()=>{await history();await refresh();}));
  $('#density-history').addEventListener('change',event=>{
    const id=event.target.value;if(!id)return;
    action(async()=>{render(await api(`${base}/${id}`));message('저장한 검토를 불러왔습니다. 원본 변경 여부는 근거 평가로 다시 확인하세요.');});
  });
  $('#density-study-panel').addEventListener('click',event=>{
    const button=event.target.closest('[data-density-job]');if(button)openJob(button.dataset.densityJob);
  });
  document.addEventListener('polymer:density-study',event=>{
    const id=event.detail?.job_id;if(!id)return;
    if (state.view !== 'doe') showView('doe');
    $('#density-source-job').value=id;$('#density-job-ids').value=id;
    $('#density-create-fields').open=true;
    $('#density-study-panel').scrollIntoView({block:'start'});
  });
  document.addEventListener('polymer:view',event=>{if(event.detail==='doe')history().catch(error=>message(error.message,true));});
  if(state.view==='doe')history().catch(error=>message(error.message,true));
})();
