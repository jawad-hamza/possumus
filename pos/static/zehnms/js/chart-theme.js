/* Chart.js defaults for ZehnMS. Load right after chart.umd.min.js, only on pages that draw a chart.
   Series colours: window.ZehnChart.primary for an ordinary series (blue), moneyIn / moneyOut / due only where
   the figure means money received, money paid out or money owed. */
(function () {
    'use strict';
    if (!window.Chart) { return; }

    var css = getComputedStyle(document.documentElement);
    function token(name, fallback) { return (css.getPropertyValue(name) || '').trim() || fallback; }

    var ink = token('--k-ink', '#1f2937');
    var inkSoft = token('--k-ink-soft', '#5b6472');
    var grid = token('--k-divider', '#edeff2');
    var panel = token('--k-panel', '#ffffff');
    var accent = token('--k-accent', '#2563eb');
    var defaults = window.Chart.defaults;

    defaults.font.family = token('--k-font', 'sans-serif');
    defaults.font.size = 12;
    defaults.color = inkSoft;
    defaults.borderColor = grid;
    defaults.animation = false;
    defaults.maintainAspectRatio = false;
    defaults.elements.line.borderWidth = 2;
    defaults.elements.line.tension = 0.3;
    defaults.elements.line.borderColor = accent;
    defaults.elements.point.radius = 0;
    defaults.elements.point.hoverRadius = 4;
    defaults.elements.point.backgroundColor = accent;
    defaults.elements.bar.borderRadius = 4;
    defaults.elements.bar.borderSkipped = 'start';
    defaults.elements.bar.backgroundColor = accent;
    defaults.interaction.mode = 'index';
    defaults.interaction.intersect = false;
    if (defaults.scale) {
        defaults.scale.grid.color = grid;
        defaults.scale.grid.drawTicks = false;
        defaults.scale.border.display = false;
        defaults.scale.ticks.padding = 8;
    }
    defaults.plugins.legend.position = 'bottom';
    defaults.plugins.legend.labels.boxWidth = 8;
    defaults.plugins.legend.labels.boxHeight = 8;
    defaults.plugins.legend.labels.usePointStyle = true;
    defaults.plugins.legend.labels.pointStyle = 'circle';
    defaults.plugins.tooltip.backgroundColor = ink;
    defaults.plugins.tooltip.titleColor = panel;
    defaults.plugins.tooltip.bodyColor = panel;
    defaults.plugins.tooltip.cornerRadius = 6;
    defaults.plugins.tooltip.displayColors = false;
    defaults.plugins.tooltip.padding = 10;

    window.ZehnChart = {
        ink: ink,
        inkSoft: inkSoft,
        rule: grid,
        grid: grid,
        primary: accent,
        primaryTint: token('--k-badge-blue-bg', '#dbeafe'),
        neutral: token('--k-border-strong', '#cdd2d9'),
        cobalt: accent,
        moneyIn: token('--k-in', '#15803d'),
        moneyOut: token('--k-out', '#b91c1c'),
        due: token('--k-due', '#b45309')
    };
})();
