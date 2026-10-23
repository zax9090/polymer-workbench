'use strict';
(() => {
  const sessions = new Map();
  const names = { rdf: '원자 간 거리 분포 · RDF', msd: '평균 제곱 변위 · MSD', free_volume: '자유 부피', widom: '기체 삽입 · Widom' };
  const defaults = {
    rdf: { pairs: 'all-all', r_max: 5, bins: 100, every: 1, max_frames: 100, exclude_same_molecule: false },
    msd: { selection: 'heavy', every: 1, max_frames: 200, remove_drift: true, time_step_fs: '' },
    free_volume: { probes: '0, 1, 1.5', spacing: 0.3 },
    widom: { gas: 'CO2', temperature: '', insertions: 2000, every: 1, max_frames: 20, cutoff: '', seed: 1 }
  };
  const colors = ['#87d9bf', '#e0b786', '#85aace', '#d89db3', '#b9bd82', '#a899cc'];
  const number = value => Number.isFinite(value) ? escape(formatNumber(value)) : '추정 불가';
  const percent = value => Number.isFinite(value) ? number(value * 100) : '추정 불가';

  function supported(job) {
    if (job.status !== 'completed' || job.config?.route !== 'polymer_atomistic' || !['bulk', 'crosslink'].includes(job.config.atomistic_mode)) return [];
    const files = new Set((job.artifacts || []).map(file => file.name));
    if (!files.has('eq3_last.data') && !files.has('final.data')) return [];
    const methods = ['rdf', 'free_volume'];
    const gaff = job.config.atomistic_mode === 'bulk' && (job.config.atomistic_forcefield || 'gaff2') === 'gaff2' && files.has('eq3_last.data');
    if (gaff && files.has('eq3.dump')) methods.splice(1, 0, 'msd');
    if (gaff) methods.push('widom');
    return methods;
  }

  function field(name, label, value, attributes = '') {
    return `<div class="field"><label for="analysis-${name}">${label}</label><input id="analysis-${name}" name="${name}" value="${escape(value)}" ${attributes}></div>`;
  }
  function numeric(name, label, value, min, max, step = 1, optional = false) {
    return field(name, label, value, `type="number" min="${min}" max="${max}" step="${step}" ${optional ? 'placeholder="작업 설정 사용"' : 'required'}`);
  }
  function checkbox(name, label, checked) {
    return `<label class="checkbox-field"><input type="checkbox" name="${name}" ${checked ? 'checked' : ''}><span>${label}</span></label>`;
  }
  function controls(method, values) {
    let html = '';
    if (method === 'rdf') html = field('pairs', '원소 쌍 · 여러 쌍은 세미콜론으로 구분', values.pairs, 'required placeholder="C-C; C-H; heavy-heavy"') +
      `<div class="field-row">${numeric('r_max', '최대 거리 (Å)', values.r_max, 0.01, 1000, 'any')}${numeric('bins', '거리 구간 수', values.bins, 2, 2000)}</div>` +
      '<p class="field-help">최대 거리 ≤ 최소 상자 길이/2 · all: 전체, heavy: 수소 제외.</p>' +
      checkbox('exclude_same_molecule', '같은 분자 안의 원자 쌍 제외', values.exclude_same_molecule);
    if (method === 'msd') html = field('selection', '원자 선택', values.selection, 'required placeholder="heavy, all 또는 C,O"') +
      numeric('time_step_fs', '계산 시간 간격 (fs) · 비우면 작업 설정 사용', values.time_step_fs, 0.000001, 1000, 'any', true) +
      checkbox('remove_drift', '전체 셀의 질량 중심 이동 제거', values.remove_drift) +
      '<p class="field-help">짧은 궤적은 평형 확산 계수를 보증하지 않습니다.</p>';
    if (method === 'free_volume') html = field('probes', '탐침 반지름 (Å) · 쉼표로 구분', values.probes, 'required placeholder="0, 1, 1.5"') +
      numeric('spacing', '격자 간격 h (Å)', values.spacing, 0.05, 1, 'any') +
      '<p class="field-help">최종 구조 · h·2h 비교는 수렴 판정이 아닙니다.</p>';
    if (method === 'widom') html = `<div class="field-row"><div class="field"><label for="analysis-gas">기체</label><select id="analysis-gas" name="gas">${['He','H2','N2','O2','Ar','CH4','CO2'].map(gas => `<option ${values.gas === gas ? 'selected' : ''}>${gas}</option>`).join('')}</select></div>${numeric('temperature', '온도 (K) · 선택', values.temperature, 1, 5000, 'any', true)}</div>` +
      `<div class="field-row">${numeric('insertions', '프레임당 삽입 횟수', values.insertions, 100, 200000)}${numeric('seed', '난수 시드', values.seed, 1, 2147483647)}</div>` +
      numeric('cutoff', '차단 거리 (Å) · 비우면 자동', values.cutoff, 0.01, 1000, 'any', true) +
      '<p class="field-help">단일 점 기체 근사 · 기본값은 확인용 · PCFF 미지원.</p>';
    if (method !== 'free_volume') html += `<div class="field-row">${numeric('every', '프레임 추출 간격', values.every, 1, 10000)}${numeric('max_frames', '최대 분석 프레임 수', values.max_frames, method === 'msd' ? 4 : 1, 2000)}</div>`;
    return html;
  }

  function readParameters(form, method) {
    const values = Object.fromEntries(new FormData(form));
    const parameters = {};
    for (const [key, value] of Object.entries(values)) {
      if (key === 'method') continue;
      if (['pairs', 'probes'].includes(key)) {
        const parts = value.split(key === 'pairs' ? ';' : ',').map(item => item.trim());
        if (parts.some(item => !item)) throw new Error('빈 항목 없이 분석 대상을 입력하세요.');
        parameters[key] = key === 'pairs' ? parts : parts.map(Number);
        if (key === 'probes' && parameters[key].some(item => !Number.isFinite(item))) throw new Error('탐침 반지름은 숫자로 입력하세요.');
      } else if (['gas', 'selection'].includes(key)) parameters[key] = value.trim();
      else if (!['remove_drift', 'exclude_same_molecule'].includes(key) && value !== '') {
        parameters[key] = Number(value);
        if (!Number.isFinite(parameters[key])) throw new Error('계산 설정에 유한한 숫자를 입력하세요.');
      }
    }
    if (method === 'rdf') parameters.exclude_same_molecule = form.elements.exclude_same_molecule.checked;
    if (method === 'msd') parameters.remove_drift = form.elements.remove_drift.checked;
    return parameters;
  }

  function chart(xs, series, xLabel, yLabel) {
    const lines = series.map(row => ({ ...row, points: xs.map((x, i) => [x, row.values[i]]).filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y)) }));
    const points = lines.flatMap(row => row.points);
    if (points.length < 2) return '<p class="research-footnote">그래프에 표시할 데이터가 부족합니다.</p>';
    const xmin = Math.min(...points.map(p => p[0])), xmax = Math.max(...points.map(p => p[0]));
    const ymin = Math.min(0, ...points.map(p => p[1])), ymax = Math.max(...points.map(p => p[1]));
    const dx = xmax - xmin || 1, dy = ymax - ymin || 1;
    return `<svg class="curve-chart structure-chart" viewBox="0 0 570 210" role="img" aria-label="${escape(yLabel)} · ${escape(xLabel)}"><title>${escape(yLabel)} · ${escape(xLabel)}</title><line x1="65" y1="165" x2="550" y2="165"/><line x1="65" y1="28" x2="65" y2="165"/>${lines.map((row, i) => `<polyline style="stroke:${colors[i % colors.length]}" points="${row.points.map(([x, y]) => `${(65 + (x - xmin) / dx * 485).toFixed(2)},${(165 - (y - ymin) / dy * 137).toFixed(2)}`).join(' ')}"/>`).join('')}<text x="65" y="185">${number(xmin)}</text><text x="550" y="185" text-anchor="end">${number(xmax)}</text><text x="58" y="165" text-anchor="end">${number(ymin)}</text><text x="58" y="33" text-anchor="end">${number(ymax)}</text><text x="65" y="16">${escape(yLabel)}</text><text x="307" y="203" text-anchor="middle">${escape(xLabel)}</text></svg><div class="structure-legend">${lines.map((row, i) => `<span><i style="background:${colors[i % colors.length]}"></i>${escape(row.label)}</span>`).join('')}</div>`;
  }
  function metric(label, value, unit = '') {
    return `<div class="research-feature"><span>${label}</span><strong>${value}</strong><small>${unit}</small></div>`;
  }
  function resultHTML(result, request) {
    let html = '';
    if (result.analysis === 'rdf') html = chart(result.r_angstrom, result.pairs.map(pair => ({ label: pair.pair, values: pair.g })), '원자 간 거리 r (Å)', 'g(r) · 무차원') +
      `<p class="research-footnote">${number(result.frames)}개 프레임 · ${result.exclude_same_molecule ? '같은 분자 쌍 제외' : '같은 분자 쌍 포함'} · 자기 쌍 제외</p>`;
    if (result.analysis === 'msd') html = chart(result.time_ps, [{ label: result.selection, values: result.msd_angstrom2 }], '시간차 (ps)', 'MSD (Å²)') +
      `<div class="research-features">${metric('분석 프레임', number(result.frames), '개')}${metric('로그–로그 기울기', number(result.fit.loglog_exponent))}${metric('확산 계수 추정', number(result.fit.diffusion_m2_s), 'm²/s')}</div>` +
      `<p class="research-footnote">피팅 시간창: ${result.fit.window_ps.map(number).join('–')} ps. ${result.fit.diffusive_window ? '시간에 비례하는 변위 구간을 찾았습니다. 평형 도달이나 확산 계수의 수렴을 보증하지 않습니다.' : '확산 구간을 확인하지 못해 확산 계수를 표시하지 않습니다.'}</p>`;
    if (result.analysis === 'free_volume') {
      const comparison = result.grid_comparison;
      html = `<div class="research-features">${metric('반데르발스 점유율', percent(result.occupied_fraction), '%')}${metric('탐침 0 자유 부피', percent(result.fractional_free_volume_probe0), '%')}${metric('Bondi FFV', number(result.bondi_ffv), '1 − 1.3 Vw/V')}</div>` +
        `<div class="table-scroll"><table class="research-table"><thead><tr><th>탐침 (Å)</th><th>접근 가능 (%)</th><th>탐침 점유 (%)</th><th>접근 가능 차이 (%p)</th><th>탐침 점유 차이 (%p)</th></tr></thead><tbody>${result.probes.map(row => `<tr><td>${number(row.probe_radius_angstrom)}</td><td>${percent(row.accessible_fraction)}</td><td>${percent(row.occupiable_fraction)}</td><td>${percent(row.accessible_fraction_absolute_difference)}</td><td>${percent(row.occupiable_fraction_absolute_difference)}</td></tr>`).join('')}</tbody></table></div>` +
        '';
      if (comparison) html += `<p class="research-footnote">격자 ${result.grid.map(number).join(' × ')} / 비교 격자 ${comparison.coarse_grid.map(number).join(' × ')}. 실제 격자 간격: ${comparison.fine_spacing_angstrom.map(number).join(' × ')} Å / ${comparison.coarse_actual_spacing_angstrom.map(number).join(' × ')} Å.<br>h·2h 절대 차이 · 오차 상한/수렴 판정 아님.</p>`;
    }
    if (result.analysis === 'widom') html = `<div class="research-features">${metric('과잉 화학 퍼텐셜', number(result.excess_chemical_potential_kcal_mol), 'kcal/mol')}${metric('용해도 계수 추정', number(result.solubility_cm3stp_per_cm3_atm), 'cm³(STP)/(cm³·atm)')}${metric('프레임 간 표준 오차', number(result.solubility_standard_error), 'cm³(STP)/(cm³·atm)')}</div>` +
      `<p class="research-footnote">${escape(result.gas)} · ${number(result.temperature_kelvin)} K · ${number(result.frames)}개 프레임 × ${number(result.insertions_per_frame)}회 삽입.<br>LJ 근사 · 전하·고분자 이완 미포함 · 실험값 아님.</p>`;
    if (result.warnings?.length) {
      const translations = [
        ['No trajectory:', '궤적 없이 최종 구조 한 프레임을 분석했습니다.'],
        ['A near-linear MSD window', 'MSD가 시간에 비례해도 평형 도달이나 확산 계수 수렴을 보장하지 않습니다.'],
        ['The selected fit window', '선택한 시간창에서 정상 확산 구간이 확인되지 않아 확산 계수를 표시하지 않습니다.'],
        ['Box lengths vary:', '상자 크기 변화가 확산 추정에 영향을 줄 수 있습니다.'],
        ['Probe-occupiable volume', '거친 격자는 작은 빈 공간을 놓칠 수 있습니다. h·2h 차이가 작아도 수렴한 것은 아닙니다.'],
        ['Single-site LJ gas', '전하·사중극자가 없는 단일 점 기체 근사입니다. 용해도 오차의 크기와 방향은 검증되지 않았습니다.'],
        ['Plain 12-6 LJ', '12-6 LJ와 꼬리 보정을 사용하며, 삽입 중 고분자 구조는 고정합니다.'],
        ['All sampled Boltzmann factors', '유효 삽입 표본이 없어 화학 퍼텐셜을 추정할 수 없습니다. 용해도가 0이라는 뜻이 아닙니다.'],
        ['Only one structure', '구조 한 개만 사용해 프레임 간 표준 오차를 추정할 수 없습니다.']
      ];
      html += `<details class="structure-warnings"><summary>계산 주의사항 (${result.warnings.length})</summary><ul>${result.warnings.map(warning => `<li>${escape(translations.find(([prefix]) => warning.startsWith(prefix))?.[1] || warning)}</li>`).join('')}</ul></details>`;
    }
    html += `<p class="research-footnote">입력: ${escape(result.source?.data || '—')}${result.source?.trajectory ? ` · ${escape(result.source.trajectory)}` : ' · 최종 구조 한 프레임'}</p>`;
    html += `<details><summary>이 결과의 실행 설정</summary><pre>${escape(JSON.stringify(request, null, 2))}</pre></details><details><summary>분석 결과 JSON</summary><pre>${escape(JSON.stringify(result, null, 2))}</pre></details><button class="secondary-button" type="button" data-analysis-download>분석 JSON 저장 ↓</button>`;
    return '<h4 class="research-subtitle">최근 실행 결과</h4><p class="research-footnote">설정을 바꾼 뒤에는 분석을 다시 실행하세요.</p>' + html;
  }

  function remember(form, entry) {
    const values = Object.fromEntries(new FormData(form));
    delete values.method;
    for (const key of ['remove_drift', 'exclude_same_molecule']) if (form.elements[key]) values[key] = form.elements[key].checked;
    entry.values[entry.method] = values;
  }
  function render(job, target = $('#output-overview')) {
    const methods = supported(job);
    $('#job-structure-analysis', target)?.remove();
    if (!methods.length) return;
    if (!sessions.has(job.id)) sessions.set(job.id, { method: methods[0], values: {}, results: {}, requests: {}, busy: false, error: null });
    const entry = sessions.get(job.id);
    if (!methods.includes(entry.method)) entry.method = methods[0];
    const values = { ...defaults[entry.method], ...entry.values[entry.method] };
    const result = entry.results[entry.method];
    const section = document.createElement('section');
    section.id = 'job-structure-analysis';
    section.className = 'structure-analysis';
    section.innerHTML = `<div class="structure-heading"><h3>구조·궤적 분석</h3><span class="research-tag amber">연구용 · 실험적</span></div><p class="research-copy"><a href="/manual" target="_blank" rel="noopener">방법·해석 설명서 ↗</a></p><form id="structure-analysis-form"><fieldset ${entry.busy ? 'disabled' : ''}><div class="field"><label for="structure-analysis-method">분석 방법</label><select id="structure-analysis-method" name="method">${methods.map(method => `<option value="${method}" ${entry.method === method ? 'selected' : ''}>${names[method]}</option>`).join('')}</select></div>${controls(entry.method, values)}<button class="secondary-button" type="submit">${entry.busy ? '분석 중…' : '분석 실행'}</button></fieldset></form><div class="research-result" id="structure-analysis-result" aria-live="polite" aria-busy="${entry.busy}">${entry.busy ? '<p class="progress-message" role="status">구조를 분석하고 있습니다. 격자와 샘플 수에 따라 수 분이 걸릴 수 있습니다.</p>' : entry.error ? `<div class="error-message" role="alert">${escape(entry.error)}</div>` : result ? resultHTML(result, entry.requests[entry.method]) : '<p class="research-footnote">설정을 확인한 뒤 분석을 실행하세요. 결과는 JSON으로 저장할 수 있습니다.</p>'}</div>`;
    target.append(section);
    const form = $('form', section);
    form.addEventListener('input', () => remember(form, entry));
    $('[name="method"]', form).addEventListener('change', event => {
      entry.method = event.target.value;
      entry.error = null;
      render(job, target);
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (entry.busy || !form.reportValidity()) return;
      remember(form, entry);
      let parameters;
      try { parameters = readParameters(form, entry.method); } catch (error) { entry.error = error.message; render(job, target); return; }
      const method = entry.method;
      entry.busy = true;
      entry.error = null;
      render(job, target);
      try {
        entry.results[method] = await api(`/api/jobs/${encodeURIComponent(job.id)}/analysis`, { method: 'POST', body: JSON.stringify({ analysis: method, parameters }) });
        entry.requests[method] = { job_id: job.id, analysis: method, parameters, completed_at: new Date().toISOString() };
      }
      catch (error) { entry.error = error.message; }
      finally {
        entry.busy = false;
        if (state.current?.id === job.id && $('#job-structure-analysis')) render(state.current);
      }
    });
    $('[data-analysis-download]', section)?.addEventListener('click', () => {
      downloadText(`${job.id}-${entry.method}.json`, JSON.stringify({ ...result, request: entry.requests[entry.method] }, null, 2), 'application/json;charset=utf-8');
    });
  }
  window.PolymerStructureAnalysis = { render };
})();
