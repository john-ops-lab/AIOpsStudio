import { post, useAction } from "../../api";
import { useEffect, useState } from "react";
import type { Entity } from "../../types";
import { Button, JsonView, Loading, Modal, Notice } from "../ui";
export default function DeletionPreview({ path, label, onClose, onDeleted }: { path: string; label: string; onClose: () => void; onDeleted: () => void }) {
  const [preview, setPreview] = useState<Entity | null>(null), [error, setError] = useState(""); const action = useAction();
  useEffect(() => { let live = true; void post(`${path}/deletion-preview`).then((data) => { if (live) setPreview(data); }).catch((e) => { if (live) setError(e instanceof Error ? e.message : String(e)); }); return () => { live = false; }; }, [path]);
  const blocked = preview && (Array.isArray(preview.blocked) ? preview.blocked.length > 0 : Boolean(preview.blocked));
  return <Modal title={`删除${label}`} onClose={onClose}><div className="stack"><p>请核对将删除的内容。服务器会再次检查引用、权限和记录版本；预览过期或数据变化后需重新预览。</p><Notice error={error || action.error} />{!preview && !error ? <Loading /> : null}{preview ? <><JsonView title="本次影响范围" open value={preview.counts || { files: preview.file_count, bytes: preview.bytes }} />{blocked ? <p className="scope-note" role="status">当前不能删除：{(preview.reasons || []).map((r: any) => typeof r === "string" ? r : r.message || r.code).join("；") || "存在运行中任务或保留引用"}</p> : <p>确认后将删除本次范围内的内容，旧下载链接会失效。</p>}<div className="actions"><Button onClick={onClose}>取消</Button><Button variant="danger" busy={Boolean(action.busy)} disabled={Boolean(blocked) || !preview.preview_token} onClick={() => void action.act("delete", async () => { await post(`${path}/delete`, { preview_token: preview.preview_token }); onDeleted(); })}>确认删除</Button></div></> : null}</div></Modal>;
}
