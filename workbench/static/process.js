'use strict';
(() => {
  const fmt = value => Number.isFinite(value) ? escape(formatNumber(value)) : '—';
  const pct = value => Number.isFinite(value) ? `${escape(formatNumber(value * 100))}%` : '—';
  const rules = { beyond_3_sigma: '3σ 밖 1점', '2_of_3_beyond_2_sigma': '연속 3점 중 2점이 2σ 밖', '4_of_5_beyond_1_sigma': '연속 5점 중 4점이 1σ 밖', '8_on_one_side': '연속 8점이 중심선 한쪽', '6_point_trend': '연속 6점 상승·하강' };
  const issues = { missing_key: '키 값 없음', duplicate_key: '같은 키가 여러 행', invalid_value: '측정값 없음·숫자 아님', invalid_order: '순서 열을 읽을 수 없음' };
  let last = null;

  function headers(text) {
    const first = (text.replace(/^\uFEFF/, '').split(/\r?\n/, 1)[0] || '');
    return first.split(',').map(name => name.replace(/^"|"$/g, '').trim()).filter(Boolean);
  }
  function refreshColumns() {
    $('#process-columns').innerHTML = headers($('#process-csv').value).map(name => `<option value="${escape(name)}">`).join('');
  }
  function showFields() {
    const kind = $('#process-kind').value;
    $$('[data-process-kinds]').forEach(el => el.classList.toggle('hidden', !el.dataset.processKinds.split(' ').includes(kind)));
  }
  function options() {
    const kind = $('#process-kind').value, out = {};
    $$('[data-process-kinds]').forEach(el => {
      if (!el.dataset.processKinds.split(' ').includes(kind)) return;
      const input = $('input, select', el), key = input.dataset.option || input.id.replace('process-', ''), raw = input.value.trim();
      if (input.type === 'checkbox') { if (input.checked) out[key] = true; return; }
      if (!raw) return;
      if (['keys', 'factors', 'features'].includes(key)) out[key] = raw.split(',').map(v => v.trim()).filter(Boolean);
      else if (key === 'thresholds') out[key] = raw.split(',').map(v => Number(v.trim()));
      else if (input.type === 'number') {
        const number = Number(raw);
        if (!Number.isFinite(number)) throw new Error(`${$('span', el).textContent.trim()}: 숫자를 입력하세요.`);
        out[key] = ['baseline', 'folds'].includes(key) ? Math.trunc(number) : number;
      } else out[key] = raw;
    });
    return out;
  }

  function chart(values, positions, limits, flagged, label) {
    const ys = [...values, limits.upper, limits.lower, limits.centre].filter(Number.isFinite);
    const ymin = Math.min(...ys), ymax = Math.max(...ys), dy = ymax - ymin || 1;
    const xmin = positions[0], dx = (positions[positions.length - 1] - xmin) || 1;
    const x = p => (65 + (p - xmin) / dx * 485).toFixed(2), y = v => (165 - (v - ymin) / dy * 137).toFixed(2);
    const line = (v, cls) => Number.isFinite(v) ? `<line class="${cls}" x1="65" x2="550" y1="${y(v)}" y2="${y(v)}"/>` : '';
    const dots = values.map((v, i) => `<circle cx="${x(positions[i])}" cy="${y(v)}" r="${flagged.has(positions[i]) ? 4 : 2.2}" class="${flagged.has(positions[i]) ? 'signal' : ''}"/>`).join('');
    return `<svg class="curve-chart structure-chart process-chart" viewBox="0 0 570 210" role="img" aria-label="${escape(label)}"><title>${escape(label)}</title><line x1="65" y1="165" x2="550" y2="165"/><line x1="65" y1="28" x2="65" y2="165"/>${line(limits.upper, 'limit')}${line(limits.lower, 'limit')}${line(limits.centre, 'centre')}<polyline points="${values.map((v, i) => `${x(positions[i])},${y(v)}`).join(' ')}"/>${dots}<text x="58" y="165" text-anchor="end">${fmt(ymin)}</text><text x="58" y="33" text-anchor="end">${fmt(ymax)}</text><text x="65" y="16">${escape(label)}</text><text x="550" y="185" text-anchor="end">순서 ${escape(String(positions[positions.length - 1]))}</text></svg>`;
  }
  const table = (head, rows) => `<div class="table-scroll"><table class="research-table"><thead><tr>${head.map(h => `<th>${escape(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  const features = items => `<div class="research-features">${items.map(([label, value, note]) => `<div class="research-feature"><span>${escape(label)}</span><strong>${value}</strong>${note ? `<small>${escape(note)}</small>` : ''}</div>`).join('')}</div>`;
  const unit = result => result.unit ? ` ${escape(result.unit)}` : '';

  function dictionaryHTML(r) {
    let html = features([['행 수', fmt(r.rows)], ['한 행의 뜻', escape(r.observation_unit || '확인 필요'), r.observation_unit ? '' : '키 누락·중복을 먼저 정리하세요'], ['계층 균형', r.balanced ? '균형' : '불균형']]);
    html += table(['키', '고유 값', '그룹', '상위당 하위 수', '상위 없이 고유'], r.levels.map(l => [escape(l.column), fmt(l.distinct_values), fmt(l.groups), `${fmt(l.children_per_parent.min)}~${fmt(l.children_per_parent.max)}`, l.unique_without_parent === undefined ? '—' : l.unique_without_parent ? '예' : '아니오 · 상위 키와 함께 연결']));
    if (r.value) html += `<p class="research-footnote">측정값 ${escape(r.value.column)}${r.value.unit ? ` (${escape(r.value.unit)})` : ''}: 숫자 ${fmt(r.value.numeric)}개, 없음·숫자 아님 ${fmt(r.value.missing_or_invalid)}개, 범위 ${fmt(r.value.min)} ~ ${fmt(r.value.max)}</p>`;
    if (r.issues.length) html += table(['문제', '개수', 'CSV 행'], r.issues.map(i => [escape(issues[i.code] || i.code), fmt(i.count ?? 1), escape((i.lines || (i.examples || []).flatMap(e => e.lines)).slice(0, 10).join(', '))]));
    else html += '<div class="success-message">키 누락·중복·측정값 오류가 없습니다.</div>';
    return html;
  }
  function chartHTML(r) {
    const flagged = new Set(r.points.filter(p => p.signals.length).map(p => p.position));
    const name = r.type === 'xbar_s' ? `부분군 평균 X̄ (n=${r.subgroup_size})` : '개별값 I';
    let html = features([['중심선', fmt(r.centre) + unit(r)], ['관리 상한 UCL', fmt(r.upper)], ['관리 하한 LCL', fmt(r.lower)], ['σ 추정', fmt(r.sigma_estimate.value), r.sigma_estimate.method], ['신호 점', fmt(flagged.size)]]);
    html += chart(r.points.map(p => p.value), r.points.map(p => p.position), r, flagged, name);
    const d = r.dispersion;
    html += chart(d.values, d.positions, d, new Set(d.signals), r.type === 'xbar_s' ? '부분군 표준편차 S' : '이동 범위 MR');
    const signalled = r.points.filter(p => p.signals.length);
    if (signalled.length) html += table(['순서', r.type === 'xbar_s' ? '부분군' : 'CSV 행', '값', '규칙'], signalled.slice(0, 200).map(p => [fmt(p.position), escape(String(p.label)), fmt(p.value), escape(p.signals.map(s => rules[s] || s).join(' · '))]));
    return html;
  }
  function capabilityHTML(r) {
    const w = r.within, o = r.overall;
    return features([['Cp', fmt(w.cp)], ['Cpk', fmt(w.cpk), `단기 σ ${formatNumber(w.sigma)}`], ['Pp', fmt(o.pp)], ['Ppk', fmt(o.ppk), `95% ${formatNumber(o.ppk_95_interval[0])} ~ ${formatNumber(o.ppk_95_interval[1])}`]]) +
      table(['항목', '단기(within)', '전체(overall)'], [['σ', fmt(w.sigma), fmt(o.sigma)], ['σ 방법', escape(w.method), escape(o.method)], ['예상 규격 밖 ppm', fmt(w.expected_ppm), fmt(o.expected_ppm)]]) +
      `<p class="research-footnote">평균 ${fmt(r.mean)}${unit(r)} · n = ${fmt(r.n)} · 실제 규격 밖 ${fmt(r.observed.outside_spec)}개 (${fmt(r.observed.ppm)} ppm)${r.normality ? ` · Shapiro–Wilk p = ${fmt(r.normality.p_value)}` : ''}</p>`;
  }
  function varianceHTML(r) {
    return `<p class="research-footnote">평균 ${fmt(r.grand_mean)}${unit(r)} · n = ${fmt(r.n)} · 분산 단위 ${escape(r.variance_unit || '측정값 단위의 제곱')}</p>` +
      table(['요인', '자유도', '평균제곱', 'F', 'p', '분산 성분', '비율', '표준편차'], r.anova.map(a => [escape(a.source === 'residual' ? '잔차(최하위 반복)' : a.source), fmt(a.df), fmt(a.ms), fmt(a.f), fmt(a.p_value), fmt(a.variance_component) + (a.truncated_negative_estimate ? ' <small>음수 → 0</small>' : ''), Number.isFinite(a.percent_of_total) ? `${fmt(a.percent_of_total)}%` : '—', fmt(a.standard_deviation)])) +
      `<details><summary>기대 평균제곱</summary><pre>${escape(r.anova.map(a => `${a.source}: ${a.expected_mean_square}`).join('\n'))}</pre></details>`;
  }
  function ratesHTML(r) {
    let html = table(['그룹', '불량/검사', '전체 불량률', '표준화 불량률'], r.groups.map(g => [escape(g.group), `${fmt(g.defects)} / ${fmt(g.inspected)}`, pct(g.crude_rate), pct(g.standardized_rate)]));
    html += table(['층', ...r.groups.map(g => g.group)], r.strata.map((s, i) => [escape(s), ...r.groups.map(g => { const c = g.strata[i]; return `${pct(c.rate)} <small>${fmt(c.defects)}/${fmt(c.inspected)} · 그룹 내 ${pct(c.share_of_group)}</small>`; })]));
    html += table(['비교', '위험비 RR', '오즈비 OR (95%)', 'CMH p', '층 간 동질성 p', '방향 역전'], r.comparisons.map(c => [`${escape(c.group)} vs ${escape(c.reference)}`, fmt(c.risk_ratio), `${fmt(c.odds_ratio)}${c.odds_ratio_95_interval ? ` <small>${fmt(c.odds_ratio_95_interval[0])} ~ ${fmt(c.odds_ratio_95_interval[1])}</small>` : ''}`, fmt(c.cmh_p_value), fmt(c.breslow_day?.p_value), c.direction_reverses_within_strata ? '<span class="research-tag amber">예 · 심슨 역설</span>' : '아니오']));
    return html;
  }
  function classificationHTML(r) {
    const row = (name, c) => [name, `${fmt(c.true_positive)} / ${fmt(r.positives)}`, fmt(c.false_negative), fmt(c.false_positive), pct(c.accuracy), pct(c.recall), pct(c.precision), pct(c.alarm_rate), c.cost === undefined ? '—' : fmt(c.cost)];
    let html = features([['행 수', fmt(r.n)], ['불량 비율', pct(r.prevalence)], ['ROC AUC', fmt(r.roc_auc), '0.5는 무작위 수준'], ['평균 정밀도 AP', fmt(r.average_precision), `무작위 수준 ${formatNumber(r.prevalence)}`]]);
    html += table(['판정', '잡은 불량', '놓침', '오탐', '정확도', '불량 검출률', '정밀도', '경고 비율', '비용'],
      [...r.thresholds.map(t => row(`점수 ≥ ${fmt(t.threshold)}`, t)), row('모두 정상 판정', { ...r.flag_nothing_baseline, cost: r.costs?.flag_nothing })]);
    if (r.costs) {
      const m = r.costs.minimum;
      html += `<p class="research-footnote">놓침 1건 ${fmt(r.costs.miss)} · 오탐 1건 ${fmt(r.costs.false_alarm)} 기준 최소 비용 판정: ${m.threshold === null ? '경고하지 않음' : `점수 ≥ ${fmt(m.threshold)}`} → 비용 ${fmt(m.cost)} (놓침 ${fmt(m.false_negative)}, 오탐 ${fmt(m.false_positive)}). 모두 경고하면 ${fmt(r.costs.flag_everything)}.</p>`;
    }
    return html;
  }
  function regressionHTML(r) {
    const o = r.overall, b = r.boundary, d = r.decisions;
    let html = features([['전체 MAE', fmt(o.mae) + unit(r)], ['RMSE', fmt(o.rmse)], ['평균 편향', fmt(o.bias), '예측 − 실측'], ['R²', fmt(o.r_squared)], ...(b ? [['경계 구간 MAE', fmt(b.mae), `±${formatNumber(b.band)} 안 ${formatNumber(b.n)}행`]] : [])]);
    if (d) html += table(['판정', '실측 규격 밖', '예측 규격 밖', '놓친 규격 밖', '오탐', '판정 일치율'], [['예측값으로 합격·불합격', fmt(d.actual_out_of_spec), fmt(d.predicted_out_of_spec), fmt(d.missed_out_of_spec), fmt(d.false_alarms), pct(d.agreement)]]);
    return html;
  }
  function splitHTML(r) {
    const names = { random: '무작위', group: '로트(그룹) 분리', time_forward: '시간 순서(과거 → 미래)' }, classify = r.task === 'classification';
    return `<p class="research-footnote">${escape(r.model)} · ${fmt(r.n)}행 · 입력 변수 ${fmt(r.features)}개</p>` +
      table(['분할', '시험 행', classify ? 'ROC AUC' : 'MAE', classify ? '불량 검출률' : 'RMSE', classify ? '균형 정확도' : '', '무작위가 낙관적'],
        Object.entries(r.schemes).map(([key, v]) => [escape(names[key] || key), fmt(v.tested_rows), fmt(classify ? v.roc_auc : v.mae), classify ? pct(v.recall) : fmt(v.rmse), classify ? pct(v.balanced_accuracy) : '', v.random_is_optimistic === undefined ? '—' : v.random_is_optimistic ? '<span class="research-tag amber">예 · 누수 의심</span>' : '아니오']));
  }
  const renderers = { process_dictionary: dictionaryHTML, control_chart: chartHTML, process_capability: capabilityHTML, variance_components: varianceHTML, stratified_rates: ratesHTML,
                      classification_metrics: classificationHTML, regression_metrics: regressionHTML, split_validation: splitHTML };

  function render(result) {
    last = result;
    const warnings = result.warnings?.length ? `<div class="structure-warnings"><strong>해석 시 주의</strong><ul>${result.warnings.map(w => `<li>${escape(w)}</li>`).join('')}</ul></div>` : '';
    $('#process-result').innerHTML = renderers[result.analysis](result) + warnings +
      `<p class="research-footnote">${escape(result.method || '')} · 입력 SHA-256 ${escape(result.input.sha256.slice(0, 16))}…</p><details><summary>결과 JSON</summary><pre>${escape(JSON.stringify(result, null, 2))}</pre></details><button class="secondary-button" type="button" id="process-download">결과 JSON 저장 ↓</button>`;
    $('#process-download').addEventListener('click', () => downloadText(`process-${result.analysis}.json`, JSON.stringify(last, null, 2), 'application/json;charset=utf-8'));
  }

  function example() {
    const kind = $('#process-kind').value, set = (key, value) => { $(`#process-${key}`).value = value; };
    $$('[data-process-kinds] input').forEach(input => { if (input.type === 'checkbox') input.checked = false; else input.value = ''; });
    let seed = 7;
    const uniform = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; };
    const normal = () => { let s = 0; for (let i = 0; i < 12; i++) s += uniform(); return s - 6; };
    const csv = rows => { $('#process-csv').value = rows.join('\n') + '\n'; };
    if (kind === 'stratified_rates') {
      csv(['tool,product,defects,inspected', 'A,general,18,900', 'A,hard,12,100', 'B,general,1,100', 'B,hard,90,900']);
      set('group', 'tool'); set('stratum', 'product'); set('defects', 'defects'); set('inspected', 'inspected');
    } else if (kind === 'classification_metrics') {
      // 10 defects in 1000 parts: cut-off 0.7 misses 4 with 5 false alarms, 0.3 misses 1 with 30.
      const rows = ['defect,score'], add = (label, score, count) => { for (let i = 0; i < count; i++) rows.push(`${label},${score}`); };
      add(1, 0.8, 6); add(1, 0.5, 3); add(1, 0.1, 1); add(0, 0.8, 5); add(0, 0.5, 25); add(0, 0.1, 960);
      csv(rows); set('label', 'defect'); set('score', 'score'); set('positive', '1'); set('thresholds', '0.7, 0.3'); set('miss_cost', 1000); set('false_alarm_cost', 10);
    } else if (kind === 'regression_metrics') {
      const rows = ['thickness_nm,vm_nm'];
      for (let i = 0; i < 200; i++) { const actual = 100 + normal() * 3, edge = Math.abs(actual - 100) > 3.5; rows.push(`${actual.toFixed(2)},${(edge ? 100 + (actual - 100) * 0.6 : actual + normal() * 0.4).toFixed(2)}`); }
      csv(rows); set('actual', 'thickness_nm'); set('predicted', 'vm_nm'); set('lsl', 95); set('usl', 105); set('band', 1.5); set('unit', 'nm');
    } else if (kind === 'split_validation') {
      const dims = 20, header = ['lot', 'time', ...Array.from({ length: dims }, (_, j) => `f${j}`), 'defect'], rows = [header.join(',')];
      let time = 0;
      for (let lot = 0; lot < 20; lot++) {
        const centre = Array.from({ length: dims }, () => normal() * 3), bad = uniform() < 0.35;
        for (let i = 0; i < 20; i++) rows.push([`L${lot}`, ++time, ...centre.map(c => (c + normal()).toFixed(3)), uniform() < (bad ? 0.45 : 0.03) ? 1 : 0].join(','));
      }
      csv(rows); set('target', 'defect'); set('positive', '1'); set('split-group', 'lot'); set('order', 'time');
      set('features', header.slice(2, -1).join(', ')); $('#process-task').value = 'classification';
    } else {
      const rows = ['run,zone,wafer,thickness_A'];
      for (let run = 1; run <= 12; run++) { const r = normal() * 17; for (let zone = 1; zone <= 4; zone++) { const z = normal() * 15; for (let wafer = 1; wafer <= 2; wafer++) rows.push(`${run},${zone},${wafer},${(563 + r + z + normal() * 11).toFixed(1)}`); } }
      csv(rows);
      set('value', 'thickness_A'); set('unit', 'Å'); set('keys', 'run, zone, wafer'); set('factors', 'run, zone');
      if (kind !== 'dictionary' && kind !== 'variance_components') set('subgroup', 'run');
      if (kind === 'capability') { set('lsl', 460); set('usl', 660); }
    }
    refreshColumns();
    toast('합성 예제 데이터입니다. 실제 측정값이 아닙니다.');
  }

  $('#process-kind').addEventListener('change', showFields);
  $('#process-csv').addEventListener('input', refreshColumns);
  $('#process-file').addEventListener('change', async event => {
    const file = event.target.files[0];
    if (!file) return;
    if (file.size > 20_000_000) return toast('CSV는 20 MB 이하여야 합니다.', true);
    $('#process-csv').value = await file.text(); refreshColumns();
  });
  $('#process-example').addEventListener('click', example);
  $('#process-form').addEventListener('submit', async event => {
    event.preventDefault();
    const output = $('#process-result');
    output.setAttribute('aria-busy', 'true');
    output.innerHTML = '<p role="status">분석 중…</p>';
    try {
      const result = await api('/api/research/process', { method: 'POST', body: JSON.stringify({ kind: $('#process-kind').value, text: $('#process-csv').value, options: options() }) });
      render(result);
    } catch (error) {
      output.innerHTML = `<div class="error-message" role="alert">${escape(error.message)}</div>`;
    } finally { output.removeAttribute('aria-busy'); }
  });
  showFields();
})();
