import { BATCH_LIMIT, BATCH_LIMIT_HINT, batchTone } from "./batch";

export function BatchCount({ count }: { count: number }) {
  const tone = batchTone(count);
  return (
    <span className={`batch-count ${tone}`} title={BATCH_LIMIT_HINT}>
      {count}/{BATCH_LIMIT}
    </span>
  );
}
