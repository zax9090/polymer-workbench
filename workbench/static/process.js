'use strict';
(() => {
  const fmt = value => Number.isFinite(value) ? escape(formatNumber(value)) : '—';
  const pct = value => Number.isFinite(value) ? `${escape(formatNumber(value * 100))}%` : '—';
  const rules = { beyond_3_sigma: '3σ 밖 1점', '2_of_3_beyond_2_sigma': '연속 3점 중 2점이 2σ 밖', '4_of_5_beyond_1_sigma': '연속 5점 중 4점이 1σ 밖', '8_on_one_side': '연속 8점이 중심선 한쪽', '6_point_trend': '연속 6점 상승·하강' };
  const issues = { missing_key: '키 값 없음', duplicate_key: '같은 키가 여러 행', invalid_value: '측정값 없음·숫자 아님', invalid_order: '순서 열을 읽을 수 없음' };
  let last = null;

  function headers(text) {
    const first = (text.replace(/^﻿/, '').split(/\r?\n/, 1)[0] || '');
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
      const input = $('input', el), key = input.id.replace('process-', ''), raw = input.value.trim();
      if (!raw) return;
      if (key === 'keys' || key === 'factors') out[key] = raw.split(',').map(v => v.trim()).filter(Boolean);
      else if (input.type === 'number') {
        const number = Number(raw);
        if (!Number.isFinite(number)) throw new Error(`${$('span', el).textContent.trim()}: 숫자를 입력하세요.`);
        out[key] = key === 'baseline' ? Math.trunc(number) : number;
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
  const renderers = { process_dictionary: dictionaryHTML, control_chart: chartHTML, process_capability: capabilityHTML, variance_components: varianceHTML, stratified_rates: ratesHTML };

  function render(result) {
    last = result;
    const warnings = result.warnings?.length ? `<div class="structure-warnings"><strong>해석 시 주의</strong><ul>${result.warnings.map(w => `<li>${escape(w)}</li>`).join('')}</ul></div>` : '';
    $('#process-result').innerHTML = renderers[result.analysis](result) + warnings +
      `<p class="research-footnote">${escape(result.method || '')} · 입력 SHA-256 ${escape(result.input.sha256.slice(0, 16))}…</p><details><summary>결과 JSON</summary><pre>${escape(JSON.stringify(result, null, 2))}</pre></details><button class="secondary-button" type="button" id="process-download">결과 JSON 저장 ↓</button>`;
    $('#process-download').addEventListener('click', () => downloadText(`process-${result.analysis}.json`, JSON.stringify(last, null, 2), 'application/json;charset=utf-8'));
  }

  function example() {
    const kind = $('#process-kind').value, set = (key, value) => { $(`#process-${key}`).value = value; };
    $$('[data-process-kinds] input').forEach(input => { input.value = ''; });
    if (kind === 'stratified_rates') {
      $('#process-csv').value = 'tool,product,defects,inspected\nA,general,18,900\nA,hard,12,100\nB,general,1,100\nB,hard,90,900\n';
      set('group', 'tool'); set('stratum', 'product'); set('defects', 'defects'); set('inspected', 'inspected');
    } else {
      let seed = 7;
      const normal = () => { let s = 0; for (let i = 0; i < 12; i++) { seed = (seed * 1103515245 + 12345) % 2147483648; s += seed / 2147483648; } return s - 6; };
      const rows = ['run,zone,wafer,thickness_A'];
      for (let run = 1; run <= 12; run++) { const r = normal() * 17; for (let zone = 1; zone <= 4; zone++) { const z = normal() * 15; for (let wafer = 1; wafer <= 2; wafer++) rows.push(`${run},${zone},${wafer},${(563 + r + z + normal() * 11).toFixed(1)}`); } }
      $('#process-csv').value = rows.join('\n') + '\n';
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
    if (file.size > 5_000_000) return toast('CSV는 5 MB 이하여야 합니다.', true);
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
