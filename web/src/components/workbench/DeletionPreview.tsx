import { ApiError, post, useAction } from "../../api";
import { useEffect, useState } from "react";
import type { Entity } from "../../types";
import { Button, JsonView, Loading, Modal, Notice } from "../ui";
export default function DeletionPreview({ path, label, onClose, onDeleted }: { path: string; label: string; onClose: () => void; onDeleted: () => void }) {
  const [preview, setPreview] = useState<Entity | null>(null), [error, setError] = useState("");
  const [started, setStarted] = useState(false), [result, setResult] = useState<Entity | null>(null);
  const action = useAction();
  useEffect(() => {
    let live = true;
    setPreview(null); setError(""); setStarted(false); setResult(null);
    void post(`${path}/deletion-preview`).then((data) => { if (live) setPreview(data); }).catch((e) => { if (live) setError(e instanceof Error ? e.message : String(e)); });
    return () => { live = false; };
  }, [path]);
  const blocked = preview && (Array.isArray(preview.blocked) ? preview.blocked.length > 0 : Boolean(preview.blocked));
  async function confirm() {
    await action.act("delete", async () => {
      setStarted(true);
      try {
        const value = await post(`${path}/delete`, { preview_token: preview!.preview_token });
        setResult(value);
        if (value.cleanup_status === "complete") onDeleted();
      } catch (failure) {
        // Definite rejections leave the preview unconsumed. An uncertain network
        // response must retain the same token for the server's idempotent retry.
        if (failure instanceof ApiError && failure.status >= 400 && failure.status < 500) setStarted(false);
        throw failure;
      }
    });
  }
  return <Modal title={`删除${label}`} onClose={onClose} disableClose={started || Boolean(action.busy)}><div className="stack">
    <p>请核对将删除的内容。服务器会再次检查引用、权限和记录版本；预览过期或数据变化后需重新预览。</p>
    <Notice error={error || action.error} />
    {!preview && !error ? <Loading /> : null}
    {preview ? <>
      <JsonView title="本次影响范围" open value={preview.counts || { files: preview.file_count, bytes: preview.bytes }} />
      {blocked ? <p className="scope-note" role="status">当前不能删除：{(preview.reasons || []).map((r: any) => typeof r === "string" ? r : r.message || r.code).join("；") || "存在运行中任务或保留引用"}</p>
        : started ? <p className="scope-note" role="status">{result?.cleanup_status === "pending" ? "内容访问已撤回，文件或检查点回收尚未完成。" : "删除结果尚未确认。"}请保留此窗口并重试，继续使用同一回收凭据。</p>
        : <p>确认后将删除本次范围内的内容，旧下载链接会失效。</p>}
      {result?.error ? <Notice error={result.error.detail || result.error.message || "回收未完成，请重试"} /> : null}
      <div className="actions"><Button disabled={started || Boolean(action.busy)} onClick={onClose}>取消</Button><Button variant="danger" busy={Boolean(action.busy)} disabled={Boolean(blocked) || !preview.preview_token} onClick={() => void confirm()}>{started ? "重试回收" : "确认删除"}</Button></div>
    </> : null}
  </div></Modal>;
}
