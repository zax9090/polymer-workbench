'use strict';
(() => {
  const recent = new Map();
  const fmt = value => Number.isFinite(value) ? escape(formatNumber(value)) : '—';
  const labels = { factorial: '2수준 요인', central_composite: '중심 합성', linear: '주효과', interaction: '상호작용', quadratic: '2차 반응표면' };
  let design = null, analysis = null;

  function addFactor(factor = {}) {
    const rows = $$('#doe-factors tr');
    if (rows.length >= 6) return toast('인자는 최대 6개입니다.', true);
    const row = document.createElement('tr');
    row.innerHTML = `<td><input aria-label="인자 이름" data-factor="name" required maxlength="60" value="${escape(factor.name || `factor_${String.fromCharCode(97 + rows.length)}`)}"></td><td><input aria-label="인자 하한" data-factor="low" type="number" step="any" required value="${escape(factor.low ?? -1)}"></td><td><input aria-label="인자 상한" data-factor="high" type="number" step="any" required value="${escape(factor.high ?? 1)}"></td><td><button type="button" class="text-button" aria-label="인자 삭제">×</button></td>`;
    $('button', row).addEventListener('click', () => {
      if ($$('#doe-factors tr').length <= 2) return toast('인자는 2개 이상 필요합니다.', true);
      row.remove(); predictionFields();
    });
    $('#doe-factors').append(row); predictionFields();
  }
  function factors() {
    const rows = $$('#doe-factors tr').map(row => {
      const low = $('[data-factor=low]', row).value.trim(), high = $('[data-factor=high]', row).value.trim();
      if (!low || !high) throw new Error('각 인자의 하한과 상한을 입력하세요.');
      return { name: $('[data-factor=name]', row).value.trim(), low: Number(low), high: Number(high) };
    });
    if (rows.some(row => !row.name || !Number.isFinite(row.low) || !Number.isFinite(row.high) || row.low >= row.high)) throw new Error('인자 이름과 하한 < 상한을 확인하세요.');
    if (new Set(rows.map(row => row.name)).size !== rows.length) throw new Error('인자 이름이 중복되었습니다.');
    return rows;
  }
  function headers(text) {
    const result = []; let field = '', quoted = false;
    for (let i = 0; i < text.length; i++) {
      const char = text[i];
      if (char === '"') { if (quoted && text[i + 1] === '"') { field += '"'; i++; } else quoted = !quoted; }
      else if (char === ',' && !quoted) { result.push(field.trim()); field = ''; }
      else if ((char === '\n' || char === '\r') && !quoted) break;
      else field += char;
    }
    result.push(field.trim());
    return result.filter(Boolean);
  }
  function updateResponse() {
    const input = $('#doe-response'), previous = input.value;
    const columns = headers($('#doe-csv').value.replace(/^\uFEFF/, ''));
    input.innerHTML = (columns.length ? columns : ['response']).map(column => `<option value="${escape(column)}">${escape(column)}</option>`).join('');
    input.value = columns.includes(previous) ? previous : columns.includes('response') ? 'response' : columns.at(-1) || 'response';
  }
  function warnings(result) {
    if (!result.warnings?.length) return '';
    const translations = [
      ['This is an experimental plan', '아직 실행하지 않은 실험 설계입니다. 실행 가능성과 인자 단위를 확인하세요.'],
      ['Runs are randomized', '실행 순서는 무작위입니다. 반복은 독립된 실험으로 수행하세요.'],
      ['Face-centred CCD', '면 중심 CCD는 지정 범위 안에서 설계하며 회전성을 보장하지 않습니다.'],
      ['Two-level factorial', '2수준 설계로는 각 인자의 제곱항을 따로 식별할 수 없습니다. 2차 모델에는 중심 합성 설계를 사용하세요.'],
      ['The recommended settings', '추천 조건은 미검증 후보이며 전역 최적해를 보장하지 않습니다. 새로운 독립 실험으로 확인하세요.'],
      ['Coefficient confidence intervals', '신뢰구간은 독립성·정규성·등분산·올바른 모델 가정을 따릅니다. 행이나 이상값을 자동 삭제하지 않습니다.'],
      ['Optimization is constrained', '추천은 지정 범위 안이지만 실제 관측한 조합 밖일 수 있습니다. 외삽과 실행 가능성을 확인하세요.'],
      ['All observed responses are constant', '반응값이 모두 같아 R²와 인자 효과의 유의성을 해석할 수 없습니다.'],
      ['Residual error is at numerical precision', '잔차가 수치 정밀도 수준이므로 유의성 검사를 생략했습니다. 실험 정밀도가 입증된 것은 아닙니다.'],
      ['Fewer than five residual', '잔차 자유도가 5 미만입니다. 독립 반복 실험을 늘려 불확실성을 확인하세요.'],
      ['The model matrix is poorly conditioned', '모델 행렬이 불안정해 작은 측정 변화에도 계수가 크게 달라질 수 있습니다.'],
      ['The candidate extrapolates', '일부 인자의 추천값이 실제 관측 범위 밖입니다. 확인 실험이 필요합니다.'],
      ['The bounded search did not reach', '지정 범위의 탐색에서 목표값에 도달하지 못했습니다. 찾은 후보 중 가장 가까운 조건입니다.']
    ];
    const status = [];
    if (result.kind === 'factorial') status.push('2수준 설계 · 제곱항 식별 불가');
    if (result.warnings.some(item => item.startsWith('Residual error is at numerical precision'))) status.push('정확 적합 · 유의성 검사 생략');
    if (result.warnings.some(item => item.startsWith('All observed responses are constant'))) status.push('반응값 일정 · R² 해석 불가');
    if (result.warnings.some(item => item.startsWith('The model matrix is poorly conditioned'))) status.push('계수 추정 불안정');
    if (result.recommendation?.extrapolated_factors?.length) status.push('관측 범위 밖 추천');
    if (result.warnings.some(item => item.startsWith('The bounded search did not reach'))) status.push('목표값 미도달');
    return `${status.length ? `<div class="research-actions">${status.map(item => `<span class="research-tag amber">${escape(item)}</span>`).join('')}</div>` : ''}<details class="structure-warnings"><summary>계산 주의사항 (${result.warnings.length})</summary><ul>${result.warnings.map(item => `<li>${escape(translations.find(([prefix]) => item.startsWith(prefix))?.[1] || item)}</li>`).join('')}</ul></details>`;
  }
  function downloads(result) {
    return `<div class="research-actions">${result.csv_download_url ? `<a class="secondary-button" href="${escape(safeURL(result.csv_download_url))}" download>CSV 저장 ↓</a>` : ''}${result.download_url ? `<a class="secondary-button" href="${escape(safeURL(result.download_url))}" download>결과 JSON ↓</a>` : ''}</div>`;
  }
  function resultHeader(result, isDesign) {
    const parameters = result.parameters || {}, method = isDesign ? result.kind || parameters.kind : result.model || parameters.model;
    const bounds = result.factors || parameters.factors || [];
    const row = (name, value) => `<div class="research-kv"><span>${escape(name)}</span><strong>${escape(value)}</strong></div>`;
    let settings = bounds.map(factor => row(factor.name, `${formatNumber(factor.low)} ~ ${formatNumber(factor.high)}`)).join('');
    if (isDesign) {
      if (parameters.center_points != null) settings += row('중심점', parameters.center_points);
      if (parameters.replicates != null) settings += row('반복', parameters.replicates);
      if ((result.seed ?? parameters.seed) != null) settings += row('무작위 시드', result.seed ?? parameters.seed);
    } else {
      const objective = result.objective || parameters.objective;
      settings += row('반응 열', result.response || parameters.response || '—');
      settings += row('목적', { maximize: '최대화', minimize: '최소화', target: '목표값' }[objective] || objective || '—');
      if (objective === 'target') settings += row('목표값', parameters.target ?? result.recommendation?.target ?? '—');
    }
    return `<h3 class="doe-result-heading">저장된 ${isDesign ? '설계' : '분석'} · ${escape(labels[method] || method || '결과')}</h3><details class="doe-result-settings"><summary>이 결과의 입력 조건</summary>${settings}</details>`;
  }
  function designHTML(result) {
    return `${resultHeader(result, true)}<div class="research-kv"><span>실험 횟수</span><strong>${fmt(result.total_runs)}회</strong></div>${downloads(result)}<div class="table-scroll"><table class="research-table"><thead><tr><th>순서</th>${result.factors.map(f => `<th>${escape(f.name)}</th>`).join('')}<th>점 종류</th></tr></thead><tbody>${result.runs.slice(0, 30).map(run => `<tr><td>${fmt(run.run_order)}</td>${result.factors.map(f => `<td>${fmt(run.values[f.name])}</td>`).join('')}<td>${escape({factorial:'요인점',center:'중심점',centre:'중심점',axial:'축점'}[run.point_type] || run.point_type)}</td></tr>`).join('')}</tbody></table></div>${result.runs.length > 30 ? '<p class="research-footnote">처음 30행 표시 · 전체 설계는 CSV에 저장됩니다.</p>' : ''}${warnings(result)}<button type="button" class="secondary-button" data-connect-design="${escape(result.id)}">벌크 계산에 연결 →</button><button type="button" class="text-button" id="doe-use-design">이 설계 CSV로 측정값 입력 →</button>`;
  }
  function scatter(observed, fitted) {
    if (!Array.isArray(observed) || !Array.isArray(fitted)) return '';
    const points = observed.map((x, i) => [x, fitted[i]]).filter(pair => pair.every(Number.isFinite));
    if (!points.length) return '';
    const values = points.flat(), min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
    const x = value => 58 + (value - min) / span * 450, y = value => 170 - (value - min) / span * 135;
    return `<svg class="curve-chart doe-chart" viewBox="0 0 560 215" role="img" aria-label="관측값과 모델 적합값"><title>관측값과 적합값 · 점선은 일치선</title><line x1="58" y1="170" x2="510" y2="170"/><line x1="58" y1="35" x2="58" y2="170"/><line x1="58" y1="170" x2="508" y2="35" stroke-dasharray="5 4"/>${points.slice(0, 2000).map(([a,b]) => `<circle cx="${x(a).toFixed(2)}" cy="${y(b).toFixed(2)}" r="3"/>`).join('')}<text x="58" y="20">적합값</text><text x="58" y="188">${fmt(min)}</text><text x="508" y="188" text-anchor="end">${fmt(max)}</text><text x="284" y="208" text-anchor="middle">관측값</text></svg>`;
  }
  const interval = values => Array.isArray(values) && values.length === 2 ? values.map(fmt).join(' ~ ') : '—';
  function researchSteps(result) {
    const flow = result.research_workflow;
    if (!flow) return '';
    return `<section class="doe-next-step"><h3>${flow.stage === 'model_review' ? '모델을 먼저 검토하세요' : '다음 단계: 독립 확인 실험'}</h3>${flow.issues?.length ? `<ul>${flow.issues.map(issue => `<li>${escape(issue.message)}</li>`).join('')}</ul>` : ''}${flow.next_steps?.length ? `<ol>${flow.next_steps.map(step => `<li>${escape(step)}</li>`).join('')}</ol>` : ''}</section>`;
  }
  function anovaHTML(result) {
    if (!result.anova) return '';
    const names = {regression:'회귀 모델',residual:'잔차',lack_of_fit:'적합 결여',pure_error:'순수 오차',total:'전체'};
    return `<section class="doe-assessment-section"><h3>분산분석 · ANOVA</h3><div class="table-scroll"><table class="research-table"><thead><tr><th>변동 원인</th><th>자유도</th><th>제곱합 SS</th><th>평균 제곱 MS</th><th>F</th><th>p</th></tr></thead><tbody>${Object.entries(names).filter(([key]) => result.anova[key]).map(([key,label]) => { const row = result.anova[key]; return `<tr><td>${label}</td><td>${fmt(row.df)}</td><td>${fmt(row.ss)}</td><td>${fmt(row.ms)}</td><td>${fmt(row.f_statistic)}</td><td>${fmt(row.p_value)}</td></tr>`; }).join('')}</tbody></table></div></section>`;
  }
  function modelComparison(result) {
    if (!result.model_comparison?.length) return '';
    return `<section class="doe-assessment-section"><h3>같은 측정값의 모델 비교</h3><div class="table-scroll"><table class="research-table"><thead><tr><th>모델</th><th>항 수</th><th>RMSE</th><th>조정 R²</th><th>예측 R²</th><th>적합 결여 p</th><th>선택</th></tr></thead><tbody>${result.model_comparison.map(row => row.status === 'available' ? `<tr ${row.model === result.model ? 'class="doe-selected-model"' : ''}><td>${escape(labels[row.model] || row.model)}</td><td>${fmt(row.terms)}</td><td>${fmt(row.rmse)}</td><td>${fmt(row.adjusted_r_squared)}</td><td>${fmt(row.predicted_r_squared)}</td><td>${fmt(row.lack_of_fit_p_value)}</td><td>${row.model === result.model ? '현재 모델' : `<button type="button" class="text-button" data-doe-model="${escape(row.model)}">이 모델로 설정</button>`}</td></tr>` : `<tr><td>${escape(labels[row.model] || row.model)}</td><td colspan="6">${escape(row.reason || '이 설계에서 추정할 수 없습니다.')}</td></tr>`).join('')}</tbody></table></div></section>`;
  }
  function residualPlot(diagnostics, ordered = false) {
    if (!Array.isArray(diagnostics.residuals) || !Array.isArray(diagnostics.fitted)) return '';
    const points = diagnostics.residuals.map((residual,i) => [ordered ? i + 1 : diagnostics.fitted[i],residual,i + 1]).filter(point => point.slice(0,2).every(Number.isFinite));
    if (!points.length) return '';
    let xmin = Infinity, xmax = -Infinity, magnitude = 0;
    for (const [a,b] of points) { xmin = Math.min(xmin,a); xmax = Math.max(xmax,a); magnitude = Math.max(magnitude,Math.abs(b)); }
    const span = xmax - xmin || 1, height = magnitude || 1;
    const x = value => 60 + (value - xmin) / span * 440, y = value => 107 - value / height * 68;
    const title = ordered ? '잔차 · CSV 행 순서' : '잔차 · 적합값';
    const shown = points.filter((_,i) => i % Math.ceil(points.length / 2000) === 0);
    return `<svg class="curve-chart doe-chart" viewBox="0 0 560 215" role="img" aria-label="${title}"><title>${title}</title><line x1="60" y1="175" x2="500" y2="175"/><line x1="60" y1="35" x2="60" y2="175"/><line x1="60" y1="107" x2="500" y2="107" stroke-dasharray="5 4"/>${shown.map(([a,b,row]) => `<circle cx="${x(a).toFixed(2)}" cy="${y(b).toFixed(2)}" r="3"><title>${row}행 · 잔차 ${fmt(b)}</title></circle>`).join('')}<text x="60" y="20">잔차</text><text x="52" y="43" text-anchor="end">${fmt(magnitude)}</text><text x="52" y="111" text-anchor="end">0</text><text x="52" y="175" text-anchor="end">${fmt(-magnitude)}</text><text x="60" y="191">${fmt(xmin)}</text><text x="500" y="191" text-anchor="end">${fmt(xmax)}</text><text x="280" y="211" text-anchor="middle">${ordered ? 'CSV 행 순서' : '적합값'}</text></svg>`;
  }
  function diagnosticsHTML(result) {
    const d = result.diagnostics || {}, unusual = d.unusual_rows || [];
    let html = `<section class="doe-assessment-section"><h3>잔차 진단</h3><div class="doe-chart-grid">${residualPlot(d)}${residualPlot(d,true)}</div>`;
    if (d.unusual_rows) html += `<details ${unusual.length ? 'open' : ''}><summary>확인할 관측 행 · ${unusual.length}개</summary>${unusual.length ? `<div class="table-scroll"><table class="research-table"><thead><tr><th>관측 행</th><th>표준화 잔차</th><th>레버리지</th><th>Cook 거리</th><th>표시 이유</th></tr></thead><tbody>${unusual.slice(0,100).map(item => `<tr><td>${fmt(item.row)}</td><td>${fmt(d.standardized_residuals?.[item.row-1])}</td><td>${fmt(d.leverage?.[item.row-1])}</td><td>${fmt(d.cooks_distance?.[item.row-1])}</td><td>${[item.large_residual ? '큰 잔차' : '',item.high_leverage ? '높은 레버리지' : '',item.influential ? '높은 영향도' : ''].filter(Boolean).join(' · ')}</td></tr>`).join('')}</tbody></table></div>${unusual.length > 100 ? '<p class="research-footnote">처음 100행 표시 · 전체 진단은 결과 JSON에 있습니다.</p>' : ''}<p class="research-footnote">표시된 관측값도 모두 적합에 포함됩니다. 원인을 확인한 뒤 판단하세요.</p>` : '<p class="research-footnote">현재 기준으로 표시된 행이 없습니다. 오차 가정이 입증된 것은 아닙니다.</p>'}</details>`;
    return html + `<details><summary>관측값과 적합값</summary>${scatter(d.observed,d.fitted)}</details></section>`;
  }
  function predictionsHTML(result) {
    if (!result.predictions?.length) return '';
    return `<section class="doe-assessment-section"><h3>지정 조건 예측</h3><div class="table-scroll"><table class="research-table"><thead><tr><th>조건</th><th>예측값</th><th>평균의 95% 신뢰구간</th><th>개별 결과의 95% 예측구간</th></tr></thead><tbody>${result.predictions.map(prediction => `<tr><td>${Object.entries(prediction.settings || {}).map(([key,value]) => `${escape(key)}: ${fmt(value)}`).join('<br>')}</td><td>${fmt(prediction.predicted_response)}</td><td>${interval(prediction.confidence_interval)}</td><td>${interval(prediction.prediction_interval)}</td></tr>`).join('')}</tbody></table></div></section>`;
  }
  function analysisHTML(result) {
    const fit = result.fit || {}, recommendation = result.recommendation;
    let html = `${resultHeader(result, false)}<div class="research-features doe-fit-summary"><div class="research-feature"><span>R²</span><strong>${fmt(fit.r_squared)}</strong></div><div class="research-feature"><span>조정 R²</span><strong>${fmt(fit.adjusted_r_squared)}</strong></div><div class="research-feature"><span>예측 R² · PRESS</span><strong>${fmt(fit.predicted_r_squared)}</strong></div><div class="research-feature"><span>RMSE</span><strong>${fmt(fit.rmse)}</strong></div></div>`;
    html += researchSteps(result) + modelComparison(result) + anovaHTML(result) + diagnosticsHTML(result);
    html += `<details class="doe-assessment-section"><summary>모델 계수 · 인자 범위 −1~1로 환산</summary><div class="table-scroll"><table class="research-table"><thead><tr><th>항</th><th>계수</th><th>표준 오차</th><th>t</th><th>p</th><th>95% 신뢰구간</th></tr></thead><tbody>${(result.coefficients || []).map(coefficient => `<tr><td>${escape(coefficient.term)}</td><td>${fmt(coefficient.estimate)}</td><td>${fmt(coefficient.standard_error)}</td><td>${fmt(coefficient.t_statistic)}</td><td>${fmt(coefficient.p_value)}</td><td>${interval(coefficient.confidence_interval)}</td></tr>`).join('')}</tbody></table></div></details>`;
    if (recommendation) html += `<section class="doe-recommendation"><h3>추천 조건 <span class="research-tag amber">확인 실험 필요</span></h3>${Object.entries(recommendation.settings || {}).map(([name,value]) => `<div class="research-kv"><span>${escape(name)}</span><strong>${fmt(value)}</strong></div>`).join('')}<div class="research-kv"><span>${escape(result.response)} 예측값</span><strong>${fmt(recommendation.predicted_response)}</strong></div>${recommendation.mean_confidence_interval ? `<div class="research-kv"><span>평균의 95% 신뢰구간</span><strong>${interval(recommendation.mean_confidence_interval)}</strong></div><div class="research-kv"><span>개별 결과의 95% 예측구간</span><strong>${interval(recommendation.prediction_interval)}</strong></div>` : ''}${result.confirmation_csv ? '<button type="button" class="secondary-button" id="doe-confirmation-download">추천 조건의 확인 실험표 CSV ↓</button>' : ''}</section>`;
    return html + predictionsHTML(result) + warnings(result) + downloads(result);
  }
  function predictionFields() {
    const container = $('#doe-prediction-fields'), enabled = $('#doe-predict-enabled').checked;
    container.classList.toggle('hidden',!enabled);
    if (!enabled) { container.replaceChildren(); return; }
    const previous = new Map($$('input',container).map(input => [input.dataset.predictionFactor,input.value]));
    try { container.innerHTML = factors().map(factor => `<label class="field"><span>${escape(factor.name)}</span><input data-prediction-factor="${escape(factor.name)}" type="number" step="any" min="${factor.low}" max="${factor.high}" value="${escape(previous.get(factor.name) ?? (factor.low + (factor.high-factor.low)/2))}" required></label>`).join(''); }
    catch { container.innerHTML = '<p class="research-footnote">인자 이름과 범위를 먼저 설정하세요.</p>'; }
  }
  async function chooseModel(model) {
    if (!analysis) return;
    if (analysis.csv_download_url) {
      const response = await fetch(safeURL(analysis.csv_download_url));
      if (!response.ok) throw new Error('저장된 측정값을 불러올 수 없습니다.');
      $('#doe-csv').value = await response.text();
    }
    $('#doe-factors').replaceChildren(); (analysis.factors || []).forEach(addFactor);
    updateResponse(); $('#doe-response').value = analysis.response;
    $('#doe-model').value = model;
    $('#doe-objective').value = analysis.objective;
    $('#doe-target').value = analysis.recommendation?.target ?? '';
    $('#doe-objective').dispatchEvent(new Event('change'));
    $('#doe-predict-enabled').checked = Boolean(analysis.predictions?.length); predictionFields();
    if (analysis.predictions?.length) $$('[data-prediction-factor]').forEach(input => { input.value = analysis.predictions[0].settings[input.dataset.predictionFactor]; });
    $('#doe-analysis-form').scrollIntoView({block:'start'});
    toast('저장된 측정값과 선택 모델을 불러왔습니다. 분석·최적화를 눌러 새 결과를 저장하세요.');
  }
  async function refreshHistory() {
    try {
      const data = await api('/api/research/doe');
      $('#doe-history').innerHTML = data.records?.length ? `<div class="table-scroll"><table class="research-table"><thead><tr><th>시각</th><th>유형</th><th>열기</th></tr></thead><tbody>${data.records.map(record => `<tr><td>${escape(timeLabel(record.created_at))}</td><td>${escape(labels[record.kind || record.model || record.parameters?.kind || record.parameters?.model] || record.record_type)}</td><td><button type="button" class="text-button" data-doe-record="${escape(record.id)}">결과 열기 ↗</button></td></tr>`).join('')}</tbody></table></div>` : '<p class="research-footnote">저장한 설계·분석이 없습니다.</p>';
    } catch (error) { $('#doe-history').innerHTML = `<div class="error-message">${escape(error.message)}</div>`; }
  }
  async function execute(form, output, payload, path) {
    const button = $('button[type=submit]', form);
    if (button.disabled) return null;
    button.disabled = true;
    output.setAttribute('aria-busy', 'true');
    output.innerHTML = '<p role="status">계산 중…</p>';
    try {
      const result = await api(`/api/research/doe/${path}`, { method: 'POST', body: JSON.stringify(payload) });
      recent.set(result.id, result);
      refreshHistory();
      return result;
    } catch (error) { output.innerHTML = `<div class="error-message" role="alert">${escape(error.message)}</div>`; return null; }
    finally { button.disabled = false; output.setAttribute('aria-busy', 'false'); }
  }
  $('#doe-bulk-preset').addEventListener('click', () => {
    $('#doe-factors').replaceChildren();
    addFactor({name:'temperature_kelvin',low:280,high:340});
    addFactor({name:'build_density',low:0.6,high:1.0});
    $('#doe-design-kind').value='central_composite';
    $('#doe-model').value='quadratic';
    $('#doe-centers').value=3;
    toast('벌크용 인자 범위를 설정했습니다. 범위를 확인한 뒤 설계를 생성하세요.');
  });
  $('#doe-add-factor').addEventListener('click', () => addFactor());
  $('#doe-csv').addEventListener('input', updateResponse);
  $('#doe-predict-enabled').addEventListener('change', predictionFields);
  $('#doe-factors').addEventListener('change', predictionFields);
  $('#doe-file').addEventListener('change', async event => {
    const file = event.target.files[0]; if (!file) return;
    if (file.size > 1000000) { event.target.value = ''; return toast('1 MB 이하의 CSV를 선택하세요.', true); }
    try { $('#doe-csv').value = await file.text(); updateResponse(); }
    catch { event.target.value = ''; toast('CSV 파일을 읽지 못했습니다.', true); }
  });
  $('#doe-objective').addEventListener('change', () => {
    const target = $('#doe-objective').value === 'target';
    $('#doe-target-field').classList.toggle('hidden', !target);
    $('#doe-target').required = target;
  });
  $('#doe-design-kind').addEventListener('change', () => { $('#doe-model').value = $('#doe-design-kind').value === 'central_composite' ? 'quadratic' : 'interaction'; });
  $('#doe-design-form').addEventListener('submit', async event => {
    event.preventDefault();
    try {
      const payload = { factors: factors(), kind: $('#doe-design-kind').value, center_points: Number($('#doe-centers').value), replicates: Number($('#doe-replicates').value), seed: Number($('#doe-seed').value) };
      design = await execute(event.currentTarget, $('#doe-design-result'), payload, 'design');
      if (design) $('#doe-design-result').innerHTML = designHTML(design);
    } catch (error) { $('#doe-design-result').innerHTML = `<div class="error-message">${escape(error.message)}</div>`; }
  });
  $('#doe-analysis-form').addEventListener('submit', async event => {
    event.preventDefault();
    try {
      const payload = { factors: factors(), text: $('#doe-csv').value, response: $('#doe-response').value, model: $('#doe-model').value, objective: $('#doe-objective').value };
      if (payload.objective === 'target') payload.target = Number($('#doe-target').value);
      if ($('#doe-predict-enabled').checked) {
        const inputs = $$('[data-prediction-factor]');
        if (inputs.length !== payload.factors.length || inputs.some(input => !input.value.trim())) throw new Error('모든 인자의 예측 조건을 입력하세요.');
        payload.prediction_points = [Object.fromEntries(inputs.map(input => [input.dataset.predictionFactor,Number(input.value)]))];
      }
      analysis = await execute(event.currentTarget, $('#doe-analysis-result'), payload, 'analyze');
      if (analysis) { $('#doe-analysis-result').innerHTML = analysisHTML(analysis); $('#doe-assessment').scrollIntoView({block:'start'}); }
    } catch (error) { $('#doe-analysis-result').innerHTML = `<div class="error-message">${escape(error.message)}</div>`; }
  });
  document.addEventListener('click', async event => {
    const stageLink = event.target.closest('.doe-stage-nav a');
    if (stageLink) { event.preventDefault(); $(stageLink.getAttribute('href')).scrollIntoView({block:'start'}); }
    const selectedModel = event.target.closest('[data-doe-model]');
    if (selectedModel) { try { await chooseModel(selectedModel.dataset.doeModel); } catch (error) { toast(error.message,true); } }
    if (event.target.closest('#doe-confirmation-download') && analysis?.confirmation_csv) {
      const url = URL.createObjectURL(new Blob(['\uFEFF',analysis.confirmation_csv],{type:'text/csv;charset=utf-8'}));
      const link = document.createElement('a'); link.href = url; link.download = `confirmation-${analysis.id || 'experiment'}.csv`;
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url),1000);
    }
    if (event.target.closest('#doe-use-design') && design) { $('#doe-factors').replaceChildren(); design.factors.forEach(addFactor); $('#doe-design-kind').value = design.kind; $('#doe-model').value = design.kind === 'central_composite' ? 'quadratic' : 'interaction'; $('#doe-csv').value = design.csv; updateResponse(); $('#doe-csv').focus(); }
    const record = event.target.closest('[data-doe-record]');
    if (record) {
      try {
        const result = recent.get(record.dataset.doeRecord) || await api(`/api/research/doe/${encodeURIComponent(record.dataset.doeRecord)}`);
        if (result.runs) { design = result; $('#doe-design-result').innerHTML = designHTML(result); $('#doe-design-result').scrollIntoView({ block: 'nearest' }); }
        else { analysis = result; $('#doe-analysis-result').innerHTML = analysisHTML(result); $('#doe-analysis-result').scrollIntoView({ block: 'nearest' }); }
      } catch (error) { toast(error.message, true); }
    }
  });
  $('#doe-history-refresh').addEventListener('click', refreshHistory);
  document.addEventListener('polymer:view', event => { if (event.detail === 'doe') refreshHistory(); });
  addFactor(); addFactor();
  if (state.view === 'doe') refreshHistory();
})();
