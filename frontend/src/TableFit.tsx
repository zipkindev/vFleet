import React, { useEffect, useRef } from "react";

const MIN_SCALE = 0.55;

/** Scale a wide table down so all columns stay visible; scroll only below the floor. */
export function TableFit({ children }: { children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const root = ref.current;
    const inner = innerRef.current;
    if (!root || !inner) return;

    let frame = 0;
    let applying = false;

    const apply = () => {
      if (applying) return;
      applying = true;
      try {
        const table = inner.querySelector("table");
        if (!table) return;

        inner.style.transform = "";
        root.style.height = "";
        root.style.overflowX = "";

        const avail = root.clientWidth;
        const need = table.scrollWidth;
        if (avail <= 0 || need <= avail + 2) return;

        const scale = Math.max(MIN_SCALE, avail / need);
        inner.style.transformOrigin = "top left";
        inner.style.transform = `scale(${scale})`;
        root.style.height = `${Math.ceil(inner.scrollHeight * scale)}px`;
        root.style.overflowX = need * scale > avail + 2 ? "auto" : "hidden";
      } finally {
        applying = false;
      }
    };

    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(apply);
    };

    const ro = new ResizeObserver(schedule);
    ro.observe(root);
    window.addEventListener("resize", schedule);
    apply();
    return () => {
      cancelAnimationFrame(frame);
      ro.disconnect();
      window.removeEventListener("resize", schedule);
    };
  }, []);

  return (
    <div className="table-fit" ref={ref}>
      <div className="table-fit-inner" ref={innerRef}>
        {children}
      </div>
    </div>
  );
}
