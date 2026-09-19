/* Dashboard: today against yesterday, and the seven-day chart. */
(function () {
    'use strict';

    var doc = document;

    /* today against yesterday, as a small percentage before the comparison line */
    Array.prototype.forEach.call(doc.querySelectorAll('[data-db-move]'), function (el) {
        var now = parseFloat(el.getAttribute('data-now'));
        var then = parseFloat(el.getAttribute('data-then'));
        if (!isFinite(now) || !isFinite(then) || then <= 0) { return; }
        var change = Math.round(((now - then) / then) * 100);
        if (!change) { return; }
        el.classList.add(change > 0 ? 'db-move--up' : 'db-move--down');
        var icon = doc.createElement('i');
        icon.className = 'bi ' + (change > 0 ? 'bi-arrow-up-short' : 'bi-arrow-down-short');
        icon.setAttribute('aria-hidden', 'true');
        el.appendChild(icon);
        el.appendChild(doc.createTextNode(Math.abs(change) + '%'));
        var said = doc.createElement('span');
        said.className = 'visually-hidden';
        said.textContent = (change > 0 ? ' above' : ' below') + ' yesterday.';
        el.appendChild(said);
    });

    var canvas = doc.getElementById('salesTrendChart');
    var source = doc.getElementById('db-trend-data');
    if (!canvas || !source || !window.Chart) { return; }

    var trend;
    try { trend = JSON.parse(source.textContent); } catch (error) { return; }

    var theme = window.ZehnChart || {};
    var symbol = canvas.getAttribute('data-currency') || '';

    function amount(value) {
        return Number(value).toLocaleString('en', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    new window.Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: {
            labels: trend.labels,
            datasets: [
                {
                    label: 'Net sales',
                    data: trend.net_sales,
                    backgroundColor: theme.primary,
                    hoverBackgroundColor: theme.primary,
                    maxBarThickness: 28,
                    order: 2
                },
                {
                    type: 'line',
                    label: 'Gross profit',
                    data: trend.gross_profit,
                    borderColor: theme.moneyIn,
                    backgroundColor: theme.moneyIn,
                    borderWidth: 2,
                    pointRadius: 3,
                    pointHoverRadius: 5,
                    fill: false,
                    order: 1
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            layout: { padding: { top: 4, right: 4, bottom: 0, left: 0 } },
            plugins: {
                legend: { display: true, position: 'bottom', align: 'start' },
                tooltip: {
                    displayColors: true,
                    callbacks: {
                        label: function (item) {
                            return ' ' + item.dataset.label + '  ' + (symbol ? symbol + ' ' : '') + amount(item.parsed.y);
                        }
                    }
                }
            },
            scales: {
                x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true } },
                y: {
                    beginAtZero: true,
                    ticks: {
                        maxTicksLimit: 5,
                        callback: function (value) { return Number(value).toLocaleString('en'); }
                    }
                }
            }
        }
    });
})();
