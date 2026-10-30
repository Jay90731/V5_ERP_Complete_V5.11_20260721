const state = { ordersPage: 1, reviewsPage: 1, activeReview: null, filters: { months: [], categories: [] } };
const fmt = new Intl.NumberFormat('zh-TW');

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
}
function money(value) { return fmt.format(Number(value || 0)); }
async function api(path, options = {}) {
  const response = await fetch(path, {cache: 'no-store', ...options});
  const payload = await response.json();
  if (!response.ok) throw new Error([payload.error, payload.detail].filter(Boolean).join('：') || '處理失敗');
  return payload;
}
function showMessage(message) {
  const toast = document.querySelector('#toast');
  toast.textContent = message;
  toast.classList.add('success');
  toast.style.display = 'block';
  setTimeout(() => { toast.style.display = 'none'; toast.classList.remove('success'); }, 4000);
}
function showError(error) {
  const toast = document.querySelector('#toast');
  toast.textContent = error.message || String(error);
  toast.classList.remove('success');
  toast.style.display = 'block';
  setTimeout(() => { toast.style.display = 'none'; }, 5000);
}
function queryString(values) {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => { if (value !== '' && value != null) params.set(key, value); });
  return params.toString();
}
function fillSelect(id, values) {
  const select = document.querySelector(id);
  const first = select.options[0];
  select.replaceChildren(first);
  values.forEach(value => select.add(new Option(value, value)));
}
function categoryText(value) { return value === '..' ? '未分類' : String(value || ''); }
function categoryLabel(value) { return `<span class="category ${escapeHtml(value)}">${escapeHtml(categoryText(value))}</span>`; }
function displayDate(value) { return String(value || '').replaceAll('-', '.'); }
function sourceText(value) { return value === 'BHQ' ? 'BHQ' : value === 'ERP' ? 'ERP' : 'ERP＋BHQ'; }
function fillCategories(id, values) {
  const select = document.querySelector(id), first = select.options[0];
  select.replaceChildren(first);
  values.forEach(value => select.add(new Option(categoryText(value), value)));
}
function fillStores(values) {
  const select = document.querySelector('#order-store'), first = select.options[0];
  select.replaceChildren(first);
  values.forEach(item => select.add(new Option(`${item.store_code}｜${item.store_name}`, item.store_code)));
}
function updatedLabel(value) {
  if (!value) return '尚無更新紀錄';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-TW', {hour12: false});
}
function applyHealth(health) {
  document.querySelector('#db-status').textContent = `SQLite ${health.quick_check === 'ok' ? '正常' : '異常'}｜${health.database_name}｜最後更新 ${updatedLabel(health.last_updated_at)}`;
  document.querySelector('#formal-count').textContent = money(health.formal_orders);
  document.querySelector('#pending-count').textContent = money(health.pending);
}

async function loadFilterOptions(source = document.querySelector('#order-source').value) {
  const filters = await api(`/api/filters?${queryString({source_system: source})}`);
  state.filters = filters;
  fillSelect('#order-month', filters.months);
  fillSelect('#stats-month', filters.months);
  fillCategories('#order-category', filters.categories);
  fillStores(filters.stores);
}

function pagination(containerId, page, pages, onPage) {
  const container = document.querySelector(containerId);
  container.replaceChildren();
  const make = (label, target, active=false, disabled=false) => {
    const button = document.createElement('button'); button.textContent = label; button.disabled = disabled; button.classList.toggle('active', active);
    button.addEventListener('click', () => onPage(target)); container.append(button);
  };
  make('上一頁', page - 1, false, page <= 1);
  const start = Math.max(1, page - 2), end = Math.min(pages, start + 4);
  for (let current = start; current <= end; current++) make(String(current), current, current === page);
  make('下一頁', page + 1, false, page >= pages);
}

async function loadOrders() {
  const values = {
    source_system: document.querySelector('#order-source').value,
    store_code: document.querySelector('#order-store').value,
    date_from: document.querySelector('#date-from').value,
    date_to: document.querySelector('#date-to').value,
    month: document.querySelector('#order-month').value,
    category: document.querySelector('#order-category').value,
    order_no: document.querySelector('#order-no').value.trim(),
    product_code: document.querySelector('#product-code').value.trim(),
    product_name: document.querySelector('#product-name').value.trim(),
    page: state.ordersPage,
    page_size: document.querySelector('#orders-page-size').value
  };
  const data = await api(`/api/orders?${queryString(values)}`);
  document.querySelector('#orders-result').textContent = `共 ${fmt.format(data.total)} 筆商品、${fmt.format(data.order_count)} 張訂單，第 ${data.page} / ${data.pages} 頁`;
  document.querySelector('#source-note').textContent = `目前資料來源：${sourceText(data.source_system)}｜折後營業額統計按一張訂單加總一次。`;
  const body = document.querySelector('#orders-body'); body.replaceChildren();
  if (!data.items.length) { body.innerHTML = '<tr><td colspan="8" class="empty">沒有符合條件的商品明細</td></tr>'; }
  data.items.forEach(item => {
    const row = document.createElement('tr');
    row.innerHTML = `<td><span class="source-badge ${item.source_system.toLowerCase()}">${escapeHtml(item.source_system)}</span> ${escapeHtml(item.store_code)}</td><td>${categoryLabel(item.category)}</td><td><button type="button" class="order-link">${escapeHtml(item.order_no)}</button></td><td>${escapeHtml(displayDate(item.order_date))}</td><td>${escapeHtml(item.product_code)}</td><td class="product-name-cell">${escapeHtml(item.product_name)}</td><td class="number">${money(item.quantity)}</td><td class="number">${money(item.discounted_amount)}</td>`;
    row.querySelector('.order-link').addEventListener('click', () => openOrder(item.order_no));
    body.append(row);
  });
  pagination('#orders-pagination', data.page, data.pages, page => { state.ordersPage = page; loadOrders().catch(showError); });
}

async function openOrder(orderNo) {
  const order = await api(`/api/orders/${encodeURIComponent(orderNo)}`);
  document.querySelector('#detail-order-no').textContent = order.order_no;
  document.querySelector('#detail-meta').textContent = `${displayDate(order.order_date)}｜${categoryText(order.category)}｜${order.source_system}｜門市 ${order.store_code}`;
  document.querySelector('#detail-summary').innerHTML = `<div><span>資料來源</span><b>${escapeHtml(order.source_system)}</b></div><div><span>門市</span><b>${escapeHtml(`${order.store_code}｜${order.store_name}`)}</b></div><div><span>商品數量</span><b>${money(order.total_quantity)}</b></div><div><span>訂單折後金額</span><b>${money(order.total_discount_amount)}</b></div>`;
  document.querySelector('#detail-items').innerHTML = order.items.map(item => `<tr><td>${item.line_no}</td><td>${escapeHtml(item.gift_code)}</td><td>${escapeHtml(item.product_name)}</td><td class="number">${money(item.quantity)}</td><td class="number">${money(item.discount_amount)}</td></tr>`).join('');
  document.querySelector('#order-dialog').showModal();
}

async function loadReviews() {
  const data = await api(`/api/reviews?${queryString({keyword: document.querySelector('#review-keyword').value.trim(), status: document.querySelector('#review-status').value, page: state.reviewsPage, page_size: 30})}`);
  document.querySelector('#reviews-result').textContent = `共 ${fmt.format(data.total)} 筆，第 ${data.page} / ${data.pages} 頁`;
  const body = document.querySelector('#reviews-body'); body.replaceChildren();
  if (!data.items.length) body.innerHTML = '<tr><td colspan="6" class="empty">沒有符合條件的待確認資料</td></tr>';
  data.items.forEach(item => {
    const row = document.createElement('tr');
    const image = item.has_image ? `<a href="/api/review-images/${item.id}" target="_blank" rel="noopener">查看原圖</a>` : escapeHtml(item.image_name || '無');
    const action = item.status === 'pending' || item.status === 'retry_requested'
      ? `<button type="button" class="review-edit" data-review-id="${item.id}">人工處理</button>`
      : '已處理';
    row.innerHTML = `<td><b>${escapeHtml(item.order_no || item.source_key)}</b></td><td>${escapeHtml(item.status)}</td><td>${escapeHtml(item.reason)}</td><td>${image}</td><td>${escapeHtml(item.last_seen_at)}</td><td>${action}</td>`;
    body.append(row);
  });
  body.querySelectorAll('.review-edit').forEach(button => button.addEventListener('click', () => openReview(Number(button.dataset.reviewId)).catch(showError)));
  pagination('#reviews-pagination', data.page, data.pages, page => { state.reviewsPage = page; loadReviews().catch(showError); });
}

function reviewItemRow(item = {}) {
  const row = document.createElement('tr');
  const fields = [
    ['gift_code', 'text', '', null],
    ['product_name', 'text', '', null],
    ['quantity', 'number', 1, 1],
    ['list_price', 'number', 0, 0],
    ['total_amount', 'number', 0, 0],
    ['discount_amount', 'number', 0, 0]
  ];
  fields.forEach(([name, type, fallback, minimum]) => {
    const cell = document.createElement('td');
    const input = document.createElement('input');
    input.type = type;
    input.dataset.field = name;
    input.value = item[name] ?? fallback;
    input.required = name !== 'product_name';
    if (minimum != null) input.min = minimum;
    cell.append(input);
    row.append(cell);
  });
  const actionCell = document.createElement('td');
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'icon-button danger-outline';
  remove.textContent = '×';
  remove.title = '刪除品項';
  remove.setAttribute('aria-label', '刪除品項');
  remove.addEventListener('click', () => {
    if (document.querySelectorAll('#review-items-body tr').length <= 1) {
      showError(new Error('正式訂單至少需要一筆品項'));
      return;
    }
    row.remove();
  });
  actionCell.append(remove);
  row.append(actionCell);
  return row;
}

function addReviewItem(item = {}) {
  document.querySelector('#review-items-body').append(reviewItemRow(item));
}

async function openReview(reviewId) {
  const review = await api(`/api/reviews/${reviewId}`);
  state.activeReview = review;
  document.querySelector('#review-dialog-title').textContent = review.form.order_no || review.source_key;
  document.querySelector('#review-dialog-meta').textContent = `${review.reason}｜最後出現 ${review.last_seen_at}`;
  document.querySelector('#review-order-no').value = review.form.order_no || '';
  document.querySelector('#review-order-date').value = review.form.order_date || '';
  document.querySelector('#review-category').value = review.form.category || '';
  document.querySelector('#review-department').value = review.form.department_code || '4001';
  document.querySelector('#review-customer').value = review.form.customer_name || '';
  document.querySelector('#review-store').value = 'SR';
  document.querySelector('#review-note').value = review.processing_note || '';
  document.querySelector('#existing-order-warning').hidden = !review.existing_order;
  const dateReview = review.date_review || {};
  const datePanel = document.querySelector('#review-date-evidence');
  const hasDateEvidence = Boolean(
    dateReview.selected_date || dateReview.candidates?.length ||
    dateReview.original_database_date || dateReview.anomaly_reasons?.length
  );
  datePanel.hidden = !hasDateEvidence;
  document.querySelector('#date-evidence-selected').textContent = displayDate(dateReview.selected_date) || '無';
  document.querySelector('#date-evidence-candidates').textContent = (dateReview.candidates || []).map(displayDate).join('、') || '無';
  document.querySelector('#date-evidence-original').textContent = displayDate(dateReview.original_database_date) || '無';
  document.querySelector('#date-evidence-query').textContent = dateReview.query_date_from && dateReview.query_date_to
    ? `${displayDate(dateReview.query_date_from)} ～ ${displayDate(dateReview.query_date_to)}` : '無';
  document.querySelector('#date-evidence-suggested').textContent = displayDate(dateReview.suggested_date) || '無';
  document.querySelector('#date-evidence-reasons').textContent = (dateReview.anomaly_reasons || []).join('；') || review.reason || '無';
  const image = document.querySelector('#review-image');
  const imageLink = document.querySelector('#review-image-link');
  if (review.has_image) {
    const url = `/api/review-images/${review.id}?v=${Date.now()}`;
    image.src = url;
    image.hidden = false;
    imageLink.href = url;
    imageLink.hidden = false;
  } else {
    image.removeAttribute('src');
    image.hidden = true;
    imageLink.hidden = true;
  }
  const body = document.querySelector('#review-items-body');
  body.replaceChildren();
  (review.form.items.length ? review.form.items : [{}]).forEach(addReviewItem);
  document.querySelector('#review-dialog').showModal();
}

function collectReviewOrder() {
  return {
    order_no: document.querySelector('#review-order-no').value,
    order_date: document.querySelector('#review-order-date').value,
    category: document.querySelector('#review-category').value,
    department_code: document.querySelector('#review-department').value,
    customer_name: document.querySelector('#review-customer').value,
    store_code: 'SR',
    items: [...document.querySelectorAll('#review-items-body tr')].map(row => {
      const value = name => row.querySelector(`[data-field="${name}"]`).value;
      return {
        gift_code: value('gift_code'),
        product_name: value('product_name'),
        quantity: value('quantity'),
        list_price: value('list_price'),
        total_amount: value('total_amount'),
        discount_amount: value('discount_amount')
      };
    })
  };
}

async function submitReviewAction(action) {
  if (!state.activeReview) return;
  const form = document.querySelector('#review-form');
  if (action === 'confirm' && !form.reportValidity()) return;
  if (action === 'exclude' && !window.confirm('確定將這筆資料標記為不納入？')) return;
  if (action === 'confirm' && !window.confirm('確認資料正確並寫入 SQLite？寫入前會先自動備份。')) return;
  const buttons = document.querySelectorAll('#review-dialog button');
  buttons.forEach(button => { button.disabled = true; });
  try {
    const result = await api(`/api/reviews/${state.activeReview.id}/actions`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Wedding-Write': '1'},
      body: JSON.stringify({
        action,
        order: action === 'confirm' ? collectReviewOrder() : null,
        note: document.querySelector('#review-note').value
      })
    });
    applyHealth(result.metrics);
    document.querySelector('#review-dialog').close();
    state.activeReview = null;
    await Promise.all([loadReviews(), loadOrders()]);
    showMessage(action === 'confirm' ? `寫入成功：${result.result}` : action === 'exclude' ? '已標記不納入' : '已保留待確認');
  } finally {
    buttons.forEach(button => { button.disabled = false; });
  }
}

async function loadStatistics() {
  const month = document.querySelector('#stats-month').value;
  const source_system = document.querySelector('#stats-source').value;
  const data = await api(`/api/statistics?${queryString({month, source_system})}`);
  document.querySelector('#stat-orders').textContent = money(data.overall.order_count);
  document.querySelector('#stat-boxes').textContent = money(data.overall.box_count);
  document.querySelector('#stat-amount').textContent = money(data.overall.amount);
  const sources = Object.fromEntries(data.sources.map(item => [item.source_system, item]));
  document.querySelector('#stat-erp-orders').textContent = money(sources.ERP?.order_count || 0);
  document.querySelector('#stat-erp-amount').textContent = money(sources.ERP?.amount || 0);
  document.querySelector('#stat-bhq-orders').textContent = money(sources.BHQ?.order_count || 0);
  document.querySelector('#stat-bhq-amount').textContent = money(sources.BHQ?.amount || 0);
  document.querySelector('#store-stats').innerHTML = data.stores.map(item => `<tr><td><span class="source-badge ${item.source_system.toLowerCase()}">${escapeHtml(item.source_system)}</span></td><td>${escapeHtml(item.store_code)}</td><td>${escapeHtml(item.store_name)}</td><td class="number">${money(item.order_count)}</td><td class="number">${money(item.box_count)}</td><td class="number">${money(item.amount)}</td></tr>`).join('');
  document.querySelector('#category-stats').innerHTML = data.categories.map(item => `<tr><td>${categoryLabel(item.category)}</td><td class="number">${money(item.order_count)}</td><td class="number">${money(item.box_count)}</td><td class="number">${money(item.amount)}</td><td class="number">${money(item.average_order_amount)}</td><td class="number">${money(item.average_box_amount)}</td><td class="number">${money(item.average_order_boxes)}</td></tr>`).join('');
  document.querySelector('#monthly-stats').innerHTML = data.monthly.map(item => `<tr><td>${escapeHtml(item.month)}</td><td class="number">${money(item.order_count)}</td><td class="number">${money(item.box_count)}</td><td class="number">${money(item.amount)}</td></tr>`).join('');
  document.querySelector('#gap-stats').innerHTML = data.gaps.map(item => `<tr><td>${escapeHtml(item.month)}</td><td>${escapeHtml(item.expected_order_no)}</td><td>${escapeHtml(item.first_observed_order_no || '未取得')}</td><td>待人工補入</td></tr>`).join('') || '<tr><td colspan="4" class="empty">沒有月份缺口紀錄</td></tr>';
}

async function initialize() {
  const health = await api('/api/health');
  applyHealth(health);
  await loadFilterOptions('ALL');
  await loadOrders();
}

document.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', async () => {
  document.querySelectorAll('.tab').forEach(item => item.classList.toggle('active', item === tab));
  document.querySelectorAll('.view').forEach(view => view.classList.remove('active'));
  document.querySelector(`#${tab.dataset.view}-view`).classList.add('active');
  try { if (tab.dataset.view === 'reviews') await loadReviews(); if (tab.dataset.view === 'statistics') await loadStatistics(); } catch (error) { showError(error); }
}));
document.querySelector('#order-filters').addEventListener('submit', event => { event.preventDefault(); state.ordersPage = 1; loadOrders().catch(showError); });
document.querySelector('#review-filters').addEventListener('submit', event => { event.preventDefault(); state.reviewsPage = 1; loadReviews().catch(showError); });
document.querySelector('#clear-orders').addEventListener('click', () => { document.querySelector('#order-filters').reset(); state.ordersPage = 1; loadFilterOptions('ALL').then(loadOrders).catch(showError); });
document.querySelector('#orders-page-size').addEventListener('change', () => { state.ordersPage = 1; loadOrders().catch(showError); });
document.querySelector('#stats-month').addEventListener('change', () => loadStatistics().catch(showError));
document.querySelector('#stats-source').addEventListener('change', () => loadStatistics().catch(showError));
document.querySelector('#order-source').addEventListener('change', event => { state.ordersPage = 1; loadFilterOptions(event.target.value).then(loadOrders).catch(showError); });
document.querySelector('#close-dialog').addEventListener('click', () => document.querySelector('#order-dialog').close());
document.querySelector('#close-review-dialog').addEventListener('click', () => document.querySelector('#review-dialog').close());
document.querySelector('#add-review-item').addEventListener('click', () => addReviewItem());
document.querySelector('#review-form').addEventListener('submit', event => { event.preventDefault(); submitReviewAction('confirm').catch(showError); });
document.querySelector('#exclude-review').addEventListener('click', () => submitReviewAction('exclude').catch(showError));
document.querySelector('#hold-review').addEventListener('click', () => submitReviewAction('hold').catch(showError));
document.querySelector('#refresh-data').addEventListener('click', () => initialize().then(() => showMessage('資料已重新整理')).catch(showError));
initialize().catch(showError);
