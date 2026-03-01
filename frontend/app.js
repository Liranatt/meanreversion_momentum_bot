/**
 * app.js — Fetches data from the FastAPI backend and populates the dashboard.
 *
 * Toggle API_BASE_URL between localhost (dev) and the Heroku app (prod).
 */

// ── Configuration ─────────────────────────────────────────
const API_BASE_URL =
  location.hostname === "localhost" || location.hostname === "127.0.0.1"
    ? "http://localhost:8000"      // local dev
    : "https://algotrading.herokuapp.com";  // production (update if your Heroku app name differs)

// ── Helpers ───────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const fmt = (n, decimals = 2) =>
  n != null ? Number(n).toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals }) : "—";
const fmtUSD = (n) => (n != null ? `$${fmt(n)}` : "—");
const fmtPct = (n) => (n != null ? `${fmt(n * 100)}%` : "—");

// ── Data fetchers ─────────────────────────────────────────

async function fetchJSON(path) {
  try {
    const res = await fetch(`${API_BASE_URL}${path}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    console.error(`Fetch failed for ${path}:`, err);
    return null;
  }
}

// ── Populate summary cards ────────────────────────────────

async function loadPortfolio() {
  const json = await fetchJSON("/api/v1/portfolio/history?limit=365");
  if (!json || !json.data || json.data.length === 0) return;

  const latest = json.data[json.data.length - 1];
  $("card-equity").textContent = fmtUSD(latest.total_equity);
  $("card-cash").textContent = fmtUSD(latest.free_cash);
  $("last-updated").textContent = `Updated: ${new Date(latest.date).toLocaleDateString()}`;

  // Draw equity curve
  drawEquityChart(json.data);
}

async function loadMetrics() {
  const json = await fetchJSON("/api/v1/metrics/current");
  if (!json || !json.data) return;

  const m = json.data;
  $("card-sharpe").textContent = fmt(m.sharpe_ratio, 2);
  $("card-drawdown").textContent = fmtPct(m.max_drawdown);
  $("card-return").textContent = fmtPct(m.total_return);
  $("card-winrate").textContent = `${fmt(m.win_rate, 1)}%`;
}

async function loadTrades() {
  const json = await fetchJSON("/api/v1/trades/recent?limit=50");
  if (!json || !json.data) return;

  $("card-trades").textContent = json.count;

  const tbody = $("trades-body");
  if (json.data.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="py-4 text-center text-gray-500">No trades yet.</td></tr>`;
    return;
  }

  // Determine latest strategy type from most recent trade
  const latestStrategy = json.data[0]?.strategy || "—";
  $("card-strategy").textContent = latestStrategy.replace("_", " ");

  tbody.innerHTML = json.data
    .map((t) => {
      const pnlClass = t.pnl > 0 ? "text-green-400" : t.pnl < 0 ? "text-red-400" : "text-gray-400";
      const actionClass = t.action === "BUY" ? "text-blue-400" : "text-orange-400";
      return `
        <tr class="border-b border-gray-800 hover:bg-gray-800/50 transition">
          <td class="py-2">${new Date(t.date).toLocaleDateString()}</td>
          <td class="font-mono">${t.symbol}</td>
          <td class="${actionClass} font-semibold">${t.action}</td>
          <td>${t.quantity}</td>
          <td>${fmtUSD(t.price)}</td>
          <td class="${pnlClass}">${fmtUSD(t.pnl)}</td>
          <td class="text-gray-400 text-xs">${t.strategy || "—"}</td>
        </tr>`;
    })
    .join("");
}

// ── Chart.js equity curve ─────────────────────────────────

let equityChartInstance = null;

function drawEquityChart(data) {
  const ctx = $("equityChart").getContext("2d");
  const labels = data.map((d) => new Date(d.date).toLocaleDateString());
  const values = data.map((d) => d.total_equity);

  if (equityChartInstance) equityChartInstance.destroy();

  equityChartInstance = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Total Equity ($)",
          data: values,
          borderColor: "#3b82f6",
          backgroundColor: "rgba(59,130,246,0.08)",
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 2,
        },
      ],
    },
    options: {
      responsive: true,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (ctx) => `$${ctx.parsed.y.toLocaleString()}`,
          },
        },
      },
      scales: {
        x: {
          ticks: { color: "#6b7280", maxTicksLimit: 12 },
          grid: { color: "rgba(255,255,255,0.04)" },
        },
        y: {
          ticks: {
            color: "#6b7280",
            callback: (v) => `$${(v / 1000).toFixed(0)}k`,
          },
          grid: { color: "rgba(255,255,255,0.04)" },
        },
      },
    },
  });
}

// ── Init ──────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  loadPortfolio();
  loadMetrics();
  loadTrades();
});
