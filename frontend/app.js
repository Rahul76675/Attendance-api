const apiBase = 'http://localhost:8000';

const apiStatusEl = document.getElementById('apiStatus');
const employeeCountEl = document.getElementById('employeeCount');
const lastUpdatedEl = document.getElementById('lastUpdated');
const employeeTableBody = document.getElementById('employeeTableBody');
const refreshBtn = document.getElementById('refreshBtn');

async function fetchJson(url) {
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

function setApiStatus(ok, message) {
  apiStatusEl.textContent = message;
  apiStatusEl.classList.toggle('status-ok', ok);
  apiStatusEl.classList.toggle('status-error', !ok);
}

function renderEmployees(items) {
  employeeTableBody.innerHTML = '';

  if (!items || items.length === 0) {
    employeeTableBody.innerHTML = '<tr><td colspan="5" class="empty">No employees found.</td></tr>';
    employeeCountEl.textContent = '0';
    return;
  }

  employeeCountEl.textContent = String(items.length);

  items.forEach((employee) => {
    const row = document.createElement('tr');
    row.innerHTML = `
      <td>${employee.emp_code || '-'}</td>
      <td>${employee.name || '-'}</td>
      <td>${employee.department || '-'}</td>
      <td>${employee.shift_start || '-'} to ${employee.shift_end || '-'}</td>
      <td>${employee.joined_on || '-'}</td>
    `;
    employeeTableBody.appendChild(row);
  });
}

async function loadDashboard() {
  try {
    const health = await fetchJson(`${apiBase}/health`);
    setApiStatus(true, health.status || 'Online');
  } catch (error) {
    setApiStatus(false, 'Offline');
    employeeTableBody.innerHTML = '<tr><td colspan="5" class="empty">Backend not reachable. Start the API on localhost:8000.</td></tr>';
    return;
  }

  try {
    const employees = await fetchJson(`${apiBase}/employees?page=1&page_size=20`);
    renderEmployees(employees.items || []);
    lastUpdatedEl.textContent = new Date().toLocaleTimeString();
  } catch (error) {
    setApiStatus(false, 'API error');
    employeeTableBody.innerHTML = `<tr><td colspan="5" class="empty">${error.message}</td></tr>`;
  }
}

refreshBtn.addEventListener('click', loadDashboard);
loadDashboard();
