'use strict';
(() => {
  const fmt = value => Number.isFinite(value) ? escape(formatNumber(value)) : '—';
  const interval = values => Array.isArray(values) && values.length === 2 ? values.map(fmt).join(' ~ ') : '—';
  const kinds = { simplex_lattice: '심플렉스 격자', simplex_centroid: '심플렉스 중심', extreme_vertices: '꼭짓점 설계' };
  const types = { vertex: '꼭짓점', edge: '모서리', interior: '내부', centroid: '중심', axial: '축 점검' };
  const goals = { maximize: '최대화', minimize: '최소화', target: '목표값' };

  function headers(text) {
    const line = text.replace(/^﻿/, '').split(/\r?\n/, 1)[0] || '';
    return line.split(',').map(item => item.trim().replace(/^"|"$/g, '')).filter(Boolean);
  }
  function addComponent(component = {}) {
    const rows = $$('#mix-components tr');
    if (rows.length >= 6) return toast('성분은 최대 6개입니다.', true);
    const row = document.createElement('tr');
    row.innerHTML = `<td><input aria-label="성분 이름" data-mix="name" required maxlength="60" value="${escape(component.name || `comp_${String.fromCharCode(65 + rows.length)}`)}"></td><td><input aria-label="성분 하한" data-mix="low" type="number" step="any" min="0" max="1" required value="${escape(component.low ?? 0)}"></td><td><input aria-label="성분 상한" data-mix="high" type="number" step="any" min="0" max="1" required value="${escape(component.high ?? 1)}"></td><td><button class="text-button" type="button" aria-label="성분 삭제">×</button></td>`;
    $('button', row).addEventListener('click', () => {
      if ($$('#mix-components tr').length <= 2) return toast('성분은 2개 이상 필요합니다.', true);
      row.remove();
    });
    $('#mix-components').append(row);
  }
  function components() {
    const rows = $$('#mix-components tr').map(row => ({ name: $('[data-mix=name]', row).value.trim(), low: Number($('[data-mix=low]', row).value), high: Number($('[data-mix=high]', row).value) }));
    if (rows.some(row => !row.name || !(row.low >= 0 && row.high <= 1 && row.low < row.high))) throw new Error('성분 이름과 0 ≤ 하한 < 상한 ≤ 1을 확인하세요.');
    if (new Set(rows.map(row => row.name)).size !== rows.length) throw new Error('성분 이름이 중복되었습니다.');
    return rows;
  }
  function processFactors() {
    const rows = $$('#doe-factors tr').map(row => ({ name: $('[data-factor=name]', row).value.trim(), low: Number($('[data-factor=low]', row).value), high: Number($('[data-factor=high]', row).value) }));
    if (rows.some(row => !row.name || !(row.low < row.high))) throw new Error('실험 설계 1번의 인자 이름과 하한 < 상한을 확인하세요.');
    return rows;
  }
  function downloads(result) {
    return `<div class="research-actions">${result.csv_download_url ? `<a class="secondary-button" href="${escape(safeURL(result.csv_download_url))}" download>CSV 저장 ↓</a>` : ''}${result.download_url ? `<a class="secondary-button" href="${escape(safeURL(result.download_url))}" download>결과 JSON ↓</a>` : ''}</div>`;
  }
  function notes(result) {
    return result.warnings?.length ? `<details class="structure-warnings"><summary>계산 주의사항 (${result.warnings.length})</summary><ul>${result.warnings.map(item => `<li>${escape(item)}</li>`).join('')}</ul></details>` : '';
  }
  async function submit(form, output, path, payload) {
    const button = $('button[type=submit]', form);
    if (button.disabled) return null;
    button.disabled = true;
    output.innerHTML = '<p role="status">계산 중…</p>';
    try {
      return await api(`/api/research/doe/${path}`, { method: 'POST', body: JSON.stringify(payload) });
    } catch (error) {
      output.innerHTML = `<div class="error-message">${escape(error.message)}</div>`;
      return null;
    } finally { button.disabled = false; }
  }

  function designHTML(result) {
    const names = result.components.map(c => c.name);
    return `<div class="research-kv"><span>설계</span><strong>${escape(result.method)}</strong></div><div class="research-kv"><span>실험 횟수</span><strong>${fmt(result.total_runs)}회</strong></div>${downloads(result)}
      <div class="table-scroll"><table class="research-table"><thead><tr><th>순서</th><th>종류</th>${names.map(n => `<th>${escape(n)}</th>`).join('')}</tr></thead><tbody>${result.runs.map(run => `<tr><td>${run.run_order}</td><td>${escape(types[run.point_type] || run.point_type)}</td>${names.map(n => `<td>${fmt(run.values[n])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>${notes(result)}`;
  }

  // Scheffé terms in the order the Rust kernel uses: z_i, then z_i z_j, then z_i z_j z_k.
  function scheffe(z, coefficients, model) {
    const terms = [...z];
    if (model !== 'scheffe_linear') for (let i = 0; i < z.length; i++) for (let j = i + 1; j < z.length; j++) terms.push(z[i] * z[j]);
    if (model === 'scheffe_special_cubic') for (let i = 0; i < z.length; i++) for (let j = i + 1; j < z.length; j++) for (let k = j + 1; k < z.length; k++) terms.push(z[i] * z[j] * z[k]);
    return terms.reduce((sum, term, index) => sum + term * coefficients[index], 0);
  }
  function ternary(result) {
    if (result.components.length !== 3) return '';
    const names = result.components.map(c => c.name), { lower, upper, pseudo_scale: scale } = result.region;
    const beta = result.coefficients.map(c => c.estimate), steps = 36, cells = [];
    const corner = [[280, 24], [60, 404], [500, 404]];
    const at = z => [z[0] * corner[0][0] + z[1] * corner[1][0] + z[2] * corner[2][0], z[0] * corner[0][1] + z[1] * corner[1][1] + z[2] * corner[2][1]];
    let low = Infinity, high = -Infinity;
    for (let i = 0; i <= steps; i++) for (let j = 0; j <= steps - i; j++) {
      const z = [i / steps, j / steps, (steps - i - j) / steps], x = z.map((v, k) => lower[k] + scale * v);
      const feasible = x.every((v, k) => v >= lower[k] - 1e-9 && v <= upper[k] + 1e-9);
      const y = scheffe(z, beta, result.model);
      if (feasible) { low = Math.min(low, y); high = Math.max(high, y); }
      cells.push({ z, y, feasible });
    }
    const span = high - low || 1, ramp = window.doeColorScale;
    const dots = cells.map(cell => { const [px, py] = at(cell.z); return `<circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="7" style="fill:${cell.feasible ? ramp.color((cell.y - low) / span) : '#1b2a31'};stroke:none;opacity:1"><title>${names.map((n, k) => `${n} ${formatNumber(lower[k] + scale * cell.z[k])}`).join(' · ')} → ${formatNumber(cell.y)}</title></circle>`; }).join('');
    const best = result.recommendation?.pseudo, mark = best ? (() => { const [px, py] = at(names.map(n => best[n])); return ramp.marker(px, py, '추천 배합'); })() : '';
    const label = (i, dx, dy) => `<text x="${corner[i][0] + dx}" y="${corner[i][1] + dy}" text-anchor="middle">${escape(names[i])}</text>`;
    return `<section class="doe-assessment-section"><h3>적합 모델의 삼각 색 지도 <small>밝을수록 큰 예측값 · 고리 = 추천 · 바탕색 = 범위 밖</small></h3>
      <svg class="curve-chart doe-chart" viewBox="0 0 620 440" style="aspect-ratio:620/440;max-width:640px" role="img" aria-label="3성분 혼합물 반응 예측 삼각 색 지도">${dots}${mark}${label(0, 0, -6)}${label(1, -10, 24)}${label(2, 10, 24)}${ramp.legend('doe-ternary-ramp', 540, 24, 404, low, high)}</svg>
      <p class="research-footnote">꼭짓점은 각 성분의 유사성분 1(하한 반영)입니다. 예측값이며 관측값이 아닙니다.</p></section>`;
  }
  function analysisHTML(result) {
    const fit = result.fit, rec = result.recommendation;
    const cards = [['R²', fit.r_squared], ['조정 R²', fit.adjusted_r_squared], ['예측 R²', fit.predicted_r_squared], ['RMSE', fit.rmse], ['잔차 자유도', fit.df_residual]];
    return `<div class="research-features doe-fit-summary">${cards.map(([name, value]) => `<div class="research-feature"><span>${name}</span><strong>${fmt(value)}</strong></div>`).join('')}</div>
      <section class="doe-recommendation"><h3>추천 배합 <span class="research-tag amber">확인 실험 필요</span></h3>${Object.entries(rec.settings).map(([n, v]) => `<div class="research-kv"><span>${escape(n)}</span><strong>${fmt(v)}</strong></div>`).join('')}
      <div class="research-kv"><span>예측값</span><strong>${fmt(rec.predicted_response)}</strong></div><div class="research-kv"><span>개별 결과의 95% 예측구간</span><strong>${interval(rec.prediction_interval)}</strong></div></section>
      ${ternary(result)}
      <details class="doe-assessment-section"><summary>Scheffé 계수 · 유사성분 기준</summary><div class="table-scroll"><table class="research-table"><thead><tr><th>항</th><th>계수</th><th>표준 오차</th><th>p</th></tr></thead><tbody>${result.coefficients.map(c => `<tr><td>${escape(c.term)}</td><td>${fmt(c.estimate)}</td><td>${fmt(c.standard_error)}</td><td>${c.p_value == null ? '—' : fmt(c.p_value)}</td></tr>`).join('')}</tbody></table></div><p class="research-footnote">1차 혼합 계수는 순수 (유사)성분의 예측 반응이라 0 검정을 하지 않습니다.</p></details>
      <details class="doe-assessment-section"><summary>모델 비교</summary><div class="table-scroll"><table class="research-table"><thead><tr><th>모델</th><th>항 수</th><th>RMSE</th><th>조정 R²</th><th>예측 R²</th><th>적합 결여 p</th></tr></thead><tbody>${result.model_comparison.map(m => m.status === 'available' ? `<tr><td>${escape(m.model)}</td><td>${m.terms}</td><td>${fmt(m.rmse)}</td><td>${fmt(m.adjusted_r_squared)}</td><td>${fmt(m.predicted_r_squared)}</td><td>${fmt(m.lack_of_fit_p_value)}</td></tr>` : `<tr><td>${escape(m.model)}</td><td colspan="5">${escape(m.reason)}</td></tr>`).join('')}</tbody></table></div></details>
      ${notes(result)}${downloads(result)}`;
  }
  function addGoal(goal = {}) {
    const row = document.createElement('tr');
    const columns = headers($('#desire-csv').value), options = (columns.length ? columns : ['response']).map(c => `<option${c === goal.name ? ' selected' : ''}>${escape(c)}</option>`).join('');
    row.innerHTML = `<td><select data-goal="name">${options}</select></td><td><select data-goal="model"></select></td><td><select data-goal="goal"><option value="maximize">최대화</option><option value="minimize">최소화</option><option value="target">목표값</option></select></td>${['lower', 'target', 'upper'].map(k => `<td><input data-goal="${k}" type="number" step="any" aria-label="${k}"></td>`).join('')}<td><input data-goal="importance" type="number" min="0.1" max="10" step="0.1" value="1" aria-label="중요도"></td><td><button class="text-button" type="button" aria-label="반응 삭제">×</button></td>`;
    $('button', row).addEventListener('click', () => row.remove());
    $('#desire-goals').append(row);
    models();
  }
  function models() {
    const mixture = $('#desire-domain').value === 'components';
    const list = mixture ? [['scheffe_quadratic', 'Scheffé 2차'], ['scheffe_linear', 'Scheffé 1차'], ['scheffe_special_cubic', 'Scheffé 특수 3차']]
      : [['quadratic', '2차'], ['interaction', '상호작용'], ['linear', '주효과']];
    $$('[data-goal=model]').forEach(select => { const keep = select.value; select.innerHTML = list.map(([v, l]) => `<option value="${v}">${l}</option>`).join(''); if (list.some(([v]) => v === keep)) select.value = keep; });
  }
  function goalRows() {
    return $$('#desire-goals tr').map(row => {
      const read = key => $(`[data-goal=${key}]`, row).value, number = key => read(key) === '' ? null : Number(read(key));
      return { name: read('name'), model: read('model'), goal: read('goal'), lower: number('lower'), target: number('target'), upper: number('upper'), importance: number('importance') ?? 1 };
    });
  }
  function desirabilityHTML(result) {
    const best = result.optimum;
    return `<section class="doe-recommendation"><h3>만족도 최적 후보 <span class="research-tag amber">확인 실험 필요</span></h3><div class="research-kv"><span>종합 만족도 D</span><strong>${fmt(best.composite_desirability)}</strong></div>${Object.entries(best.settings).map(([n, v]) => `<div class="research-kv"><span>${escape(n)}</span><strong>${fmt(v)}</strong></div>`).join('')}
      <div class="research-kv"><span>관측한 실험 중 최고 D</span><strong>${fmt(result.best_observed_run.composite_desirability)} (CSV ${result.best_observed_run.row}행)</strong></div></section>
      <div class="table-scroll"><table class="research-table"><thead><tr><th>반응</th><th>목표</th><th>예측값</th><th>95% 예측구간</th><th>개별 d</th><th>중요도</th><th>예측 R²</th></tr></thead><tbody>${result.responses.map(r => `<tr><td>${escape(r.name)}</td><td>${escape(goals[r.goal])}</td><td>${fmt(r.predicted)}</td><td>${interval(r.prediction_interval)}</td><td>${fmt(r.desirability)}</td><td>${fmt(r.importance)}</td><td>${fmt(r.fit.predicted_r_squared)}</td></tr>`).join('')}</tbody></table></div>${notes(result)}${downloads(result)}`;
  }

  function init() {
    if (!$('#mix-design-form')) return;
    [{ name: 'resin_A', low: 0, high: 1 }, { name: 'resin_B', low: 0, high: 1 }, { name: 'hardener', low: 0, high: 1 }].forEach(addComponent);
    $('#mix-add-component').addEventListener('click', () => addComponent());
    $('#mix-objective').addEventListener('change', () => $('#mix-target-field').classList.toggle('hidden', $('#mix-objective').value !== 'target'));
    $('#mix-csv').addEventListener('input', () => { const columns = headers($('#mix-csv').value); $('#mix-response').innerHTML = columns.map(c => `<option${c === 'response' ? ' selected' : ''}>${escape(c)}</option>`).join(''); });
    $('#desire-csv').addEventListener('input', () => { const columns = headers($('#desire-csv').value); $$('[data-goal=name]').forEach(select => { const keep = select.value; select.innerHTML = columns.map(c => `<option>${escape(c)}</option>`).join(''); if (columns.includes(keep)) select.value = keep; }); });
    $('#desire-domain').addEventListener('change', models);
    $('#desire-add-goal').addEventListener('click', () => addGoal());
    addGoal();
    $('#mix-design-form').addEventListener('submit', async event => {
      event.preventDefault();
      const output = $('#mix-design-result');
      let payload;
      try { payload = { components: components(), kind: $('#mix-kind').value, degree: Number($('#mix-degree').value), axial: $('#mix-axial').checked, centroid: $('#mix-centroid').checked, replicates: Number($('#mix-replicates').value), seed: Number($('#mix-seed').value) }; }
      catch (error) { return toast(error.message, true); }
      const result = await submit(event.target, output, 'mixture-design', payload);
      if (!result) return;
      output.innerHTML = designHTML(result);
      $('#mix-csv').value = result.csv; $('#mix-csv').dispatchEvent(new Event('input'));
      toast(`${kinds[result.kind]} 설계를 저장했습니다. 측정값을 채운 뒤 분석하세요.`);
    });
    $('#mix-analysis-form').addEventListener('submit', async event => {
      event.preventDefault();
      const output = $('#mix-analysis-result');
      let payload;
      try {
        payload = { text: $('#mix-csv').value, components: components(), response: $('#mix-response').value, model: $('#mix-model').value, objective: $('#mix-objective').value };
        if (payload.objective === 'target') payload.target = Number($('#mix-target').value);
      } catch (error) { return toast(error.message, true); }
      const result = await submit(event.target, output, 'mixture-analyze', payload);
      if (result) output.innerHTML = analysisHTML(result);
    });
    $('#desire-form').addEventListener('submit', async event => {
      event.preventDefault();
      const output = $('#desire-result');
      let payload;
      try {
        payload = { text: $('#desire-csv').value, responses: goalRows() };
        if ($('#desire-domain').value === 'components') payload.components = components(); else payload.factors = processFactors();
      } catch (error) { return toast(error.message, true); }
      const result = await submit(event.target, output, 'desirability', payload);
      if (result) output.innerHTML = desirabilityHTML(result);
    });
  }
  document.addEventListener('DOMContentLoaded', init);
})();
