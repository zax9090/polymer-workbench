'use strict';
// Screening shows reference-data predictions; nothing here enters the measurement store without the review form.
const screeningState = { predictions: null, sphere: null, solventNames: false };
const screenDomains = { inside: ['학습 범위 안', ''], edge: ['학습 범위 경계', ' amber'], outside: ['학습 범위 밖', ' amber'] };
const screenUnit = unit => unit === 'dimensionless' ? '' : unit;
const screenNumber = (value, digits = 4) => { if (typeof value !== 'number' || !Number.isFinite(value)) return '—'; const a = Math.abs(value); return a !== 0 && (a < 1e-3 || a >= 1e5) ? value.toExponential(Math.max(digits - 2, 1)) : String(Number(value.toPrecision(digits))); };
const screenRequest = (path, body, method = 'POST') => researchRequest(`/screening${path}`, body, method);
const hspInput = id => { const value = Number($(id).value); if (!Number.isFinite(value)) throw new Error('HSP 값은 숫자여야 합니다.'); return value; };

function screenOwn(prediction, key) {
  const runs = (prediction.own_simulations || []).filter(run => run.values[key]);
  if (!runs.length) return '<span class="research-tag muted">내 벌크 계산 없음</span>';
  return runs.map(run => { const v = run.values[key]; return `<div>${screenNumber(v.value)} <small>${escape(screenUnit(v.unit))}</small> · z ${screenNumber(v.z, 2)}<br><small>${escape(run.name || run.job_id)} · ${run.force_field === 'pcff' ? 'PCFF' : 'GAFF2'} · 단계 길이 ×${escape(run.protocol_scale ?? 1)}${run.radonpy_criteria_met === false ? ' · 수렴 기준 미달' : ''}</small></div>`; }).join('');
}

function renderPredictions(data) {
  screeningState.predictions = data.predictions;
  $('#screen-to-campaign').classList.toggle('hidden', !data.predictions.length);
  $('#screen-result').innerHTML = data.predictions.map(p => {
    const [label, tone] = screenDomains[p.applicability.domain] || [p.applicability.domain, ' amber'];
    const rows = Object.entries(p.values).map(([key, v]) => `<tr><td>${escape(v.label)}</td><td><strong>${screenNumber(v.value)}</strong> ± ${screenNumber(v.std, 2)} <small>${escape(screenUnit(v.unit))}</small></td><td>${screenNumber(v.interval_95[0])} – ${screenNumber(v.interval_95[1])}</td><td>${screenOwn(p, key)}</td></tr>`).join('');
    const near = p.nearest_reference.map(n => `<li><span class="mono">${escape(n.smiles)}</span> · ${escape(n.monomer_id)} · 유사도 ${n.similarity.toFixed(2)}</li>`).join('');
    const exact = p.applicability.in_reference_set.length ? ` · PI1070에 같은 반복단위 있음 (${escape(p.applicability.in_reference_set.join(', '))})` : '';
    return `<div class="model-card"><div class="panel-title"><h3 class="mono">${escape(p.repeat_smiles)}</h3><span class="research-tag${tone}">${label} · 최대 유사도 ${p.applicability.max_similarity.toFixed(2)}</span></div>
      <p class="research-footnote">RadonPy 전원자 MD 기준 예측 · ${p.temperature_kelvin} K, ${p.pressure_atm} atm${exact}</p>
      <div class="table-scroll"><table class="research-table"><thead><tr><th>물성</th><th>예측 ± 1σ</th><th>95% 구간</th><th>이 작업공간의 벌크 MD</th></tr></thead><tbody>${rows}</tbody></table></div>
      <details><summary>가장 가까운 PI1070 고분자 · 주의 사항</summary><ul class="research-list">${near}</ul><ul class="research-list">${p.warnings.map(w => `<li>${escape(w)}</li>`).join('')}</ul></details></div>`;
  }).join('');
}

function renderValidation(data) {
  const rows = Object.entries(data.properties).map(([key, v]) => `<tr><td>${escape(v.label || key)}</td><td>${v.n}</td><td>${screenNumber(v.mae, 3)} <small>${escape(screenUnit(v.unit))}</small></td><td>${v.r2.toFixed(3)}</td><td>${Math.round(v.coverage_1sigma * 100)}% / ${Math.round(v.coverage_2sigma * 100)}%</td><td>${['inside', 'edge', 'outside'].map(b => `${screenDomains[b][0]} ${screenNumber(v.by_domain[b].mae, 2)} (${v.by_domain[b].count})`).join('<br>')}</td></tr>`).join('');
  $('#screen-result').innerHTML = `<div class="model-card screen-wide"><h3>교차검증</h3><p class="research-footnote">${escape(data.scheme)}</p><div class="table-scroll"><table class="research-table"><thead><tr><th>물성</th><th>n</th><th>MAE</th><th>R²</th><th>1σ / 2σ 포함률</th><th>유사도 구간별 MAE (개수)</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
}

function screenToCampaign() {
  const smiles = (screeningState.predictions || []).map(p => p.repeat_smiles);
  if (!smiles.length) return;
  const candidates = smiles.map(repeat => ({ name: `${repeat} · bulk`, route: 'polymer_atomistic', atomistic_mode: 'bulk', repeat_smiles: repeat, monomers: 20, chains: 4, temperature_kelvin: 300, protocol_scale: 0.01, seed: 42 }));
  $('#campaign-route').value = 'polymer_atomistic'; $('#campaign-route').dispatchEvent(new Event('change'));
  $('#campaign-smiles').value = smiles.join('\n');
  $('#campaign-candidates').value = JSON.stringify(candidates, null, 2);
  $('#campaign-budget').value = Math.min(candidates.length, 50);
  $('#campaign-objective-source').value = 'feature'; $('#campaign-objective-source').dispatchEvent(new Event('change'));
  $('#campaign-objective-name').value = 'bulk_density'; $('#campaign-objective-unit').value = 'g/cm^3';
  showView('campaigns');
  toast('RadonPy 벌크 MD 후보를 넣었습니다(단계 길이 ×0.01, 탐색용). 사슬 수·스텝·목표와 예산을 검토한 후 저장하세요.');
}

function renderSolvents(data) {
  const red = data.polymer.radius != null;
  const rows = data.solvents.map(s => `<tr><td>${escape(s.name)}</td><td>${screenNumber(s.delta_d, 3)} / ${screenNumber(s.delta_p, 3)} / ${screenNumber(s.delta_h, 3)}</td><td>${screenNumber(s.ra, 3)}</td>${red ? `<td>${screenNumber(s.red, 3)} <span class="research-tag${s.prediction === 'inside' ? '' : ' muted'}">${s.prediction === 'inside' ? '구 안' : '구 밖'}${s.boundary ? ' · 경계' : ''}</span></td>` : ''}<td>${screenNumber(s.chi, 3)}</td></tr>`).join('');
  const blendRows = list => list.map(b => `<li>${escape(b.solvents[0])} ${Math.round(b.volume_fractions[0] * 100)}% + ${escape(b.solvents[1])} ${Math.round(b.volume_fractions[1] * 100)}% → Ra ${screenNumber(b.ra, 3)}${b.red != null ? `, RED ${screenNumber(b.red, 3)}` : ''}</li>`).join('');
  const blends = data.blends ? `${data.blends.nonsolvent_pairs?.length ? `<h4>두 비용매의 혼합으로 구 안에 드는 조합</h4><ul class="research-list">${blendRows(data.blends.nonsolvent_pairs)}</ul>` : ''}<h4>중심에 가장 가까운 두 용매 혼합</h4><ul class="research-list">${blendRows(data.blends.closest)}</ul>` : '';
  $('#solvent-result').innerHTML = `<div class="table-scroll"><table class="research-table"><thead><tr><th>용매</th><th>δD / δP / δH</th><th>Ra</th>${red ? '<th>RED</th>' : ''}<th>χ</th></tr></thead><tbody>${rows}</tbody></table></div>${blends}<ul class="research-list">${data.notes.map(n => `<li>${escape(n)}</li>`).join('')}</ul><p class="research-footnote">용매 데이터: ${escape(data.solvent_source.repository)} @ ${escape(data.solvent_source.commit.slice(0, 7))} · SHA256 ${escape(data.solvent_source.sha256.slice(0, 12))}…</p>`;
}

function parseSphereTests(text) {
  return text.split(/\r?\n/).map(line => line.trim()).filter(Boolean).map((line, i) => {
    const parts = line.split(',').map(x => x.trim());
    const flag = parts[parts.length - 1];
    if (!['0', '1'].includes(flag)) throw new Error(`${i + 1}번째 줄: 마지막 값은 1(용해·팽윤) 또는 0(불용)이어야 합니다.`);
    if (parts.length === 2) return { solvent: parts[0], good: flag === '1' };
    if (parts.length === 5) { const [d, p, h] = parts.slice(1, 4).map(Number); return { name: parts[0], delta_d: d, delta_p: p, delta_h: h, good: flag === '1' }; }
    throw new Error(`${i + 1}번째 줄: "용매,1" 또는 "이름,δD,δP,δH,1" 형식이어야 합니다.`);
  });
}

function renderSphere(data) {
  screeningState.sphere = data;
  const c = data.center;
  const tests = data.tests.map(t => `<tr><td>${escape(t.solvent)}</td><td>${t.observed === 'good' ? '용해' : '불용'}</td><td>${screenNumber(t.red, 3)}</td><td>${t.correct ? '일치' : '<span class="research-tag amber">불일치</span>'}</td></tr>`).join('');
  $('#sphere-result').innerHTML = `<div class="research-value">${screenNumber(c.delta_d, 3)} / ${screenNumber(c.delta_p, 3)} / ${screenNumber(c.delta_h, 3)}<small>MPa½</small></div><p>δD / δP / δH · R0 ${screenNumber(data.radius, 3)} · DATAFIT ${data.datafit.toFixed(3)} · 정분류 ${Math.round(data.accuracy * 100)}% (${data.n_tests}개 중 용해 ${data.n_good}개)</p>
    ${data.warnings.length ? `<ul class="research-list">${data.warnings.map(w => `<li>${escape(w)}</li>`).join('')}</ul>` : ''}
    <div class="research-actions"><button class="secondary-button" type="button" id="sphere-to-solvents">이 HSP로 용매 순위 보기</button><button class="text-button" type="button" id="sphere-to-measurements">측정값 초안으로 보내기</button></div>
    <details><summary>용매별 RED</summary><div class="table-scroll"><table class="research-table"><thead><tr><th>용매</th><th>관찰</th><th>RED</th><th>판정</th></tr></thead><tbody>${tests}</tbody></table></div></details>`;
  $('#sphere-to-solvents').addEventListener('click', () => { $('#solvent-dd').value = c.delta_d.toFixed(2); $('#solvent-dp').value = c.delta_p.toFixed(2); $('#solvent-dh').value = c.delta_h.toFixed(2); $('#solvent-r0').value = data.radius.toFixed(2); $('#solvent-form').requestSubmit(); });
  $('#sphere-to-measurements').addEventListener('click', sphereToMeasurement);
}

function sphereToMeasurement() {
  const data = screeningState.sphere, draft = data?.measurement_drafts?.[0];
  if (!draft) return;
  // One form holds one value; the remaining components are listed in the toast for the next entries.
  researchState.appliedDraft = { property: draft.property, value: draft.value, unit: draft.unit, analysis: draft.analysis };
  $('#measurement-property').value = draft.property; measurementUnits(); $('#measurement-unit').value = draft.unit; $('#measurement-value').value = draft.value.toFixed(3);
  $('#measurement-method').value = draft.method; $('#measurement-conditions').value = JSON.stringify({ test: 'solvent dissolution/swelling', solvents: data.n_tests }, null, 2);
  $('#measurement-origin').value = 'experimental'; $('#measurement-review').value = 'false';
  $('#measurement-source').value = `용해 시험 ${data.n_tests}개 · Hansen 구 피팅 (${data.method_version})`;
  showView('measurements'); $('#measurement-form').scrollIntoView({ behavior: 'smooth', block: 'start' });
  toast(`δD 초안을 넣었습니다. 같은 방법으로 δP ${data.center.delta_p.toFixed(2)}, δH ${data.center.delta_h.toFixed(2)}, R0 ${data.radius.toFixed(2)} MPa½를 등록하세요. 재료와 시험 조건을 확인한 후 저장합니다.`);
}

function blendSide(side) {
  const smiles = $(`#blend-${side}-smiles`).value.trim();
  return { name: $(`#blend-${side}-name`).value.trim() || side.toUpperCase(), degree_of_polymerization: Number($(`#blend-${side}-dp`).value), ...(smiles ? { repeat_smiles: smiles } : {}),
           delta_d: hspInput(`#blend-${side}-dd`), delta_p: hspInput(`#blend-${side}-dp2`), delta_h: hspInput(`#blend-${side}-dh`) };
}

function renderBlend(data) {
  const ok = data.prediction === 'miscible';
  $('#blend-result').innerHTML = `<div class="research-value">${screenNumber(data.chi, 3)}<small>χ (V_ref ${data.reference_volume} cm³/mol)</small></div><p><span class="research-tag${ok ? '' : ' amber'}">${ok ? '상용 예측' : '상분리 예측'}</span> χ/χc ${screenNumber(data.chi_over_critical, 3)} · χc ${screenNumber(data.chi_critical, 3)} · Ra ${screenNumber(data.ra, 3)} MPa½ · ${data.temperature_kelvin} K</p>
    <ul class="research-list">${data.polymers.map(p => `<li>${escape(p.name)}: 반복단위 몰부피 ${screenNumber(p.molar_volume, 4)} cm³/mol (${escape(p.molar_volume_source)}), 세그먼트 수 N ${screenNumber(p.segments, 4)}</li>`).join('')}${data.warnings.map(w => `<li>${escape(w)}</li>`).join('')}</ul>`;
}

async function loadSolventNames() {
  if (screeningState.solventNames) return;
  const data = await screenRequest('/solvents', undefined, 'GET');
  $('#solvent-names').innerHTML = data.solvents.map(s => `<option value="${escape(s.name)}">${escape(s.cas)} · ${s.delta_d}/${s.delta_p}/${s.delta_h}</option>`).join('');
  screeningState.solventNames = true;
}

$('#screen-form').addEventListener('submit', e => { e.preventDefault(); researchAction($('button[type="submit"]', e.currentTarget), '#screen-result', async () => {
  const smiles = $('#screen-smiles').value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
  if (!smiles.length || smiles.length > 20) throw new Error('반복단위를 1~20개 입력하세요.');
  $('#screen-result').innerHTML = '<p class="research-footnote">처음 실행하면 참조 모델을 학습합니다(수 초).</p>';
  renderPredictions(await screenRequest('/predict', { smiles }));
}); });
$('#screen-validation').addEventListener('click', e => researchAction(e.currentTarget, '#screen-result', async () => { $('#screen-result').innerHTML = '<p class="research-footnote">5겹 교차검증을 계산하는 중입니다.</p>'; renderValidation(await screenRequest('/validation', undefined, 'GET')); }));
$('#screen-to-campaign').addEventListener('click', screenToCampaign);
$('#solvent-form').addEventListener('submit', e => { e.preventDefault(); researchAction($('button[type="submit"]', e.currentTarget), '#solvent-result', async () => {
  const radius = $('#solvent-r0').value.trim();
  renderSolvents(await screenRequest('/solvents', { polymer: { delta_d: hspInput('#solvent-dd'), delta_p: hspInput('#solvent-dp'), delta_h: hspInput('#solvent-dh'), ...(radius ? { radius: Number(radius) } : {}) },
    temperature: Number($('#solvent-temperature').value), limit: 25, blends: Number($('#solvent-blends').value) || 0 }));
}); });
$('#sphere-form').addEventListener('submit', e => { e.preventDefault(); researchAction($('button[type="submit"]', e.currentTarget), '#sphere-result', async () => renderSphere(await screenRequest('/hsp-fit', { tests: parseSphereTests($('#sphere-tests').value) }))); });
$$('[data-sphere-add]').forEach(button => button.addEventListener('click', () => { const name = $('#sphere-search').value.trim(); if (!name) return; const area = $('#sphere-tests'); area.value = `${area.value.replace(/\s*$/, '')}\n${name},${button.dataset.sphereAdd}`; $('#sphere-search').value = ''; }));
$('#blend-form').addEventListener('submit', e => { e.preventDefault(); researchAction($('button[type="submit"]', e.currentTarget), '#blend-result', async () => renderBlend(await screenRequest('/blend', { a: blendSide('a'), b: blendSide('b'), temperature: Number($('#blend-temperature').value), reference_volume: Number($('#blend-vref').value) }))); });
document.addEventListener('polymer:view', event => { if (event.detail === 'screening') loadSolventNames().catch(error => toast(error.message, true)); });

function renderContactChi(data) {
  const ref = data.chi_reference, phase = data.phase;
  const z = Object.entries(data.coordination).map(([pair, v]) => `Z<sub>${pair}</sub> ${screenNumber(v.mean, 3)}`).join(' · ');
  const tc = phase.critical_temperature_kelvin ? `${phase.kind} 임계 온도 ${screenNumber(phase.critical_temperature_kelvin, 4)} K` : escape(phase.kind || '');
  $('#contact-chi-result').innerHTML = `<div class="research-value">${screenNumber(ref.chi, 3)}<small>χ<sub>H</sub> at ${ref.temperature_kelvin} K ± ${screenNumber(ref.bootstrap_std, 2)} (부트스트랩)</small></div>
    <p>χ = ${screenNumber(data.fit.A, 3)} ${data.fit.B_kelvin < 0 ? '−' : '+'} ${screenNumber(Math.abs(data.fit.B_kelvin), 4)}/T · 임계 χ<sub>c</sub> ${screenNumber(phase.critical_chi, 3)} · ${tc} · 계산 ${escape(data.kernel.engine)}</p>
    <p>${z}</p>
    <div class="table-scroll"><table class="research-table"><thead><tr><th>T (K)</th><th>χ</th><th>E<sub>mix</sub> (kcal/mol)</th></tr></thead><tbody>${data.chi_vs_temperature.map(r => `<tr><td>${r.temperature_kelvin}</td><td>${screenNumber(r.chi, 3)}</td><td>${screenNumber(r.e_mix_kcal_mol, 3)}</td></tr>`).join('')}</tbody></table></div>
    <ul class="research-list">${data.warnings.map(w => `<li>${escape(w)}</li>`).join('')}</ul>`;
}

$('#contact-chi-form').addEventListener('submit', e => { e.preventDefault(); researchAction($('button[type="submit"]', e.currentTarget), '#contact-chi-result', async () => {
  const [nBase, nScreen] = $('#contact-n').value.split(',').map(x => Number(x.trim()) || 1);
  renderContactChi(await screenRequest('/blend-contacts', { base_smiles: $('#contact-base').value.trim(), screen_smiles: $('#contact-screen').value.trim(),
    samples: Number($('#contact-samples').value), clusters: Number($('#contact-clusters').value), n_base: nBase, n_screen: nScreen ?? 1 }));
}); });
