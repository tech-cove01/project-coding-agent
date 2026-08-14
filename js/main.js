/* Coding Agent 展示页 — 交互脚本
   原则：用 IntersectionObserver，不用 scroll 监听；只动 transform/opacity；尊重 reduced-motion。 */

(function () {
  "use strict";

  const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---------- 滚动渐显 ----------
  // 目标：hero 以下每个 section 的主容器，以及亮点卡 / 架构层
  const revealTargets = document.querySelectorAll(
    "#about > .wrap, #highlights > .wrap, #architecture > .wrap, #benchmark > .wrap, #screenshot > .wrap, .cta > .wrap, .hl, .layer"
  );

  const show = (el) => el.classList.add("is-visible");
  const hide = (el) => el.classList.add("reveal");

  if (prefersReduced) {
    revealTargets.forEach(show);
  } else if ("IntersectionObserver" in window) {
    revealTargets.forEach(hide);
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            show(entry.target);
            io.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.12 }
    );
    revealTargets.forEach((el) => io.observe(el));
  } else {
    revealTargets.forEach(show);
  }

  // 给亮点卡片分配 stagger 下标
  document.querySelectorAll(".section-tint .hl").forEach((el, i) => {
    el.style.setProperty("--i", Math.min(i, 5));
  });

  // ---------- 评测图表动画 ----------
  const fillEls = document.querySelectorAll(".chart-fill");
  if (prefersReduced) {
    fillEls.forEach((el) => (el.style.width = el.dataset.w + "%"));
  } else if ("IntersectionObserver" in window) {
    const chartIO = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.style.width = entry.target.dataset.w + "%";
            chartIO.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.4 }
    );
    fillEls.forEach((el) => chartIO.observe(el));
  } else {
    fillEls.forEach((el) => (el.style.width = el.dataset.w + "%"));
  }

  // ---------- 页头透明渐变处理 ----------
  const head = document.querySelector(".site-head");
  if (head) {
    const updateHead = () => {
      head.style.borderBottomColor =
        window.scrollY > 8 ? "var(--line-soft)" : "transparent";
    };
    // 直接绑定 scroll，但仅切换颜色，不影响性能
    updateHead();
    window.addEventListener("scroll", updateHead, { passive: true });
  }
})();
