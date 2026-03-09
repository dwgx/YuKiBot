import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  Input,
  Select,
  SelectItem,
  Spinner,
  Textarea,
} from "@heroui/react";
import { History, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import {
  api,
  MemoryAuditItem,
  MemoryCompactResult,
  MemoryRecordItem,
} from "../api/client";

function fmtTime(ts: string): string {
  const text = String(ts || "").trim();
  if (!text) return "-";
  const d = new Date(text);
  if (Number.isNaN(d.getTime())) return text;
  return d.toLocaleString();
}

export default function MemoryPage() {
  const [records, setRecords] = useState<MemoryRecordItem[]>([]);
  const [audits, setAudits] = useState<MemoryAuditItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [loading, setLoading] = useState(false);
  const [auditLoading, setAuditLoading] = useState(false);
  const [error, setError] = useState("");
  const [selectedId, setSelectedId] = useState<number>(0);

  const [conversationId, setConversationId] = useState("");
  const [userId, setUserId] = useState("");
  const [role, setRole] = useState("");
  const [keyword, setKeyword] = useState("");

  const [addConversationId, setAddConversationId] = useState("");
  const [addUserId, setAddUserId] = useState("");
  const [addRole, setAddRole] = useState("user");
  const [addContent, setAddContent] = useState("");
  const [addNote, setAddNote] = useState("");

  const selected = useMemo(
    () => records.find((row) => row.id === selectedId) || null,
    [records, selectedId],
  );
  const [editContent, setEditContent] = useState("");
  const [editNote, setEditNote] = useState("");
  const [deleteNote, setDeleteNote] = useState("");
  const [compactConversationId, setCompactConversationId] = useState("");
  const [compactUserId, setCompactUserId] = useState("");
  const [compactRole, setCompactRole] = useState("");
  const [compactKeepLatest, setCompactKeepLatest] = useState("1");
  const [compactNote, setCompactNote] = useState("");
  const [compactLoading, setCompactLoading] = useState(false);
  const [compactResult, setCompactResult] = useState<MemoryCompactResult | null>(null);

  useEffect(() => {
    setEditContent(selected?.content || "");
    setEditNote("");
    setDeleteNote("");
  }, [selected?.id, selected?.content]);

  const loadRecords = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.getMemoryRecords({
        conversationId: conversationId.trim(),
        userId: userId.trim(),
        role: role.trim(),
        keyword: keyword.trim(),
        page,
        pageSize,
      });
      setRecords(res.items || []);
      setTotal(Number(res.total || 0));
      if (!res.items?.find((row) => row.id === selectedId)) {
        setSelectedId(Number(res.items?.[0]?.id || 0));
      }
      setError("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "读取记忆库失败");
    } finally {
      setLoading(false);
    }
  }, [conversationId, keyword, page, pageSize, role, selectedId, userId]);

  const loadAudit = useCallback(async () => {
    setAuditLoading(true);
    try {
      const res = await api.getMemoryAudit({ recordId: selectedId || 0, page: 1, pageSize: 20 });
      setAudits(res.items || []);
      setError("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "读取审计日志失败");
    } finally {
      setAuditLoading(false);
    }
  }, [selectedId]);

  useEffect(() => {
    loadRecords();
  }, [loadRecords]);

  useEffect(() => {
    loadAudit();
  }, [loadAudit]);

  const onAdd = async () => {
    if (!addConversationId.trim() || !addUserId.trim() || !addContent.trim()) {
      setError("新增记忆需要 conversation_id、user_id、content");
      return;
    }
    try {
      await api.addMemoryRecord({
        conversation_id: addConversationId.trim(),
        user_id: addUserId.trim(),
        role: addRole.trim() || "user",
        content: addContent.trim(),
        note: addNote.trim(),
        actor: "webui",
      });
      setAddContent("");
      setAddNote("");
      await loadRecords();
      await loadAudit();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "新增记忆失败");
    }
  };

  const onUpdate = async () => {
    if (!selected) return;
    if (!editNote.trim()) {
      setError("修改记忆必须填写备注");
      return;
    }
    if (!editContent.trim()) {
      setError("内容不能为空");
      return;
    }
    try {
      await api.updateMemoryRecord(selected.id, {
        content: editContent.trim(),
        note: editNote.trim(),
        actor: "webui",
      });
      await loadRecords();
      await loadAudit();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "修改记忆失败");
    }
  };

  const onDelete = async () => {
    if (!selected) return;
    if (!deleteNote.trim()) {
      setError("删除记忆必须填写备注");
      return;
    }
    try {
      await api.deleteMemoryRecord(selected.id, {
        note: deleteNote.trim(),
        actor: "webui",
      });
      setSelectedId(0);
      await loadRecords();
      await loadAudit();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "删除记忆失败");
    }
  };

  const runCompact = async (dryRun: boolean) => {
    if (!dryRun && !compactNote.trim()) {
      setError("执行整理必须填写备注 note");
      return;
    }
    setCompactLoading(true);
    try {
      const keepLatest = Math.max(1, Number.parseInt(compactKeepLatest || "1", 10) || 1);
      const res = await api.compactMemory({
        conversation_id: compactConversationId.trim(),
        user_id: compactUserId.trim(),
        role: compactRole.trim(),
        dry_run: dryRun,
        keep_latest: keepLatest,
        note: compactNote.trim(),
        actor: "webui",
      });
      setCompactResult(res.result || null);
      setError("");
      if (!dryRun) {
        await loadRecords();
        await loadAudit();
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "整理记忆失败");
    } finally {
      setCompactLoading(false);
    }
  };

  const totalPages = Math.max(1, Math.ceil(total / Math.max(1, pageSize)));

  return (
    <section className="space-y-4">
      <div className="flex items-center gap-2">
        <History size={20} />
        <h2 className="text-xl font-bold">记忆库管理</h2>
      </div>
      {error && <p className="text-danger text-sm whitespace-pre-wrap">{error}</p>}

      <Card>
        <CardHeader className="pb-1">筛选与查询</CardHeader>
        <CardBody className="grid gap-3 md:grid-cols-6">
          <Input label="会话ID（高级）" value={conversationId} onValueChange={setConversationId} />
          <Input label="QQ号（高级）" value={userId} onValueChange={setUserId} />
          <Select
            label="role"
            selectedKeys={role ? [role] : []}
            onSelectionChange={(keys) => setRole(String(Array.from(keys)[0] || ""))}
          >
            <SelectItem key="">全部</SelectItem>
            <SelectItem key="user">user</SelectItem>
            <SelectItem key="assistant">assistant</SelectItem>
            <SelectItem key="system">system</SelectItem>
          </Select>
          <Input label="关键词" value={keyword} onValueChange={setKeyword} />
          <Select
            label="每页"
            selectedKeys={[String(pageSize)]}
            onSelectionChange={(keys) => {
              setPageSize(Number(String(Array.from(keys)[0] || "50")));
              setPage(1);
            }}
          >
            {[20, 50, 100, 200].map((n) => (
              <SelectItem key={String(n)}>{String(n)}</SelectItem>
            ))}
          </Select>
          <div className="flex items-end gap-2 pb-1">
            <Button color="primary" onPress={() => { setPage(1); loadRecords(); }}>
              查询
            </Button>
            <Button
              variant="flat"
              startContent={<RefreshCw size={14} />}
              onPress={() => {
                loadRecords();
                loadAudit();
              }}
            >
              刷新
            </Button>
          </div>
        </CardBody>
      </Card>

      <Card>
        <CardHeader className="pb-1">记忆记录（{total} 条）</CardHeader>
        <CardBody className="space-y-3">
          <div className="flex items-center justify-between text-xs text-default-500">
            <span>第 {page} / {totalPages} 页</span>
            <div className="flex gap-2">
              <Button size="sm" variant="flat" isDisabled={page <= 1} onPress={() => setPage((p) => Math.max(1, p - 1))}>
                上一页
              </Button>
              <Button size="sm" variant="flat" isDisabled={page >= totalPages} onPress={() => setPage((p) => Math.min(totalPages, p + 1))}>
                下一页
              </Button>
            </div>
          </div>
          {loading ? (
            <div className="py-8 flex justify-center"><Spinner /></div>
          ) : (
            <div className="overflow-auto border border-default-300/50 rounded-xl">
              <table className="min-w-full text-sm">
                <thead className="bg-content2/60">
                  <tr>
                    <th className="text-left px-3 py-2">ID</th>
                    <th className="text-left px-3 py-2">昵称</th>
                    <th className="text-left px-3 py-2">场景</th>
                    <th className="text-left px-3 py-2">角色</th>
                    <th className="text-left px-3 py-2">内容</th>
                    <th className="text-left px-3 py-2">时间</th>
                  </tr>
                </thead>
                <tbody>
                  {records.map((row) => (
                    <tr
                      key={row.id}
                      className={`border-t border-default-200/40 cursor-pointer ${row.id === selectedId ? "bg-primary/10" : ""}`}
                      onClick={() => setSelectedId(row.id)}
                    >
                      <td className="px-3 py-2 whitespace-nowrap">#{row.id}</td>
                      <td className="px-3 py-2 whitespace-nowrap">{row.display_name || row.user_id}</td>
                      <td className="px-3 py-2 whitespace-nowrap">{row.conversation_label || row.conversation_id}</td>
                      <td className="px-3 py-2 whitespace-nowrap">{row.role}</td>
                      <td className="px-3 py-2 break-words max-w-[420px]">{row.content}</td>
                      <td className="px-3 py-2 whitespace-nowrap">{fmtTime(row.created_at)}</td>
                    </tr>
                  ))}
                  {records.length === 0 && (
                    <tr><td colSpan={6} className="px-3 py-8 text-center text-default-500">暂无数据</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader className="pb-1">新增记忆</CardHeader>
        <CardBody className="grid gap-3 md:grid-cols-2">
          <Input label="会话ID（高级）" value={addConversationId} onValueChange={setAddConversationId} />
          <Input label="QQ号" value={addUserId} onValueChange={setAddUserId} />
          <Select
            label="role"
            selectedKeys={[addRole]}
            onSelectionChange={(keys) => setAddRole(String(Array.from(keys)[0] || "user"))}
          >
            <SelectItem key="user">user</SelectItem>
            <SelectItem key="assistant">assistant</SelectItem>
            <SelectItem key="system">system</SelectItem>
          </Select>
          <Input label="备注 note（可选）" value={addNote} onValueChange={setAddNote} />
          <Textarea className="md:col-span-2" label="内容 content" minRows={3} value={addContent} onValueChange={setAddContent} />
          <div className="md:col-span-2">
            <Button color="primary" startContent={<Plus size={14} />} onPress={onAdd}>新增</Button>
          </div>
        </CardBody>
      </Card>

      <Card>
        <CardHeader className="pb-1">编辑/删除（必须备注）</CardHeader>
        <CardBody className="space-y-3">
          <div className="text-xs text-default-500">
            当前记录: {selected ? `#${selected.id}` : "未选择"}
          </div>
          <Textarea
            label="修改后的内容"
            minRows={3}
            value={editContent}
            onValueChange={setEditContent}
            isDisabled={!selected}
          />
          <Input label="修改备注 note（必填）" value={editNote} onValueChange={setEditNote} isDisabled={!selected} />
          <div className="flex gap-2">
            <Button color="primary" startContent={<Save size={14} />} isDisabled={!selected} onPress={onUpdate}>
              保存修改
            </Button>
          </div>
          <Input
            label="删除备注 note（必填）"
            value={deleteNote}
            onValueChange={setDeleteNote}
            isDisabled={!selected}
          />
          <Button color="danger" variant="flat" startContent={<Trash2 size={14} />} isDisabled={!selected} onPress={onDelete}>
            删除记录
          </Button>
        </CardBody>
      </Card>

      <Card>
        <CardHeader className="pb-1">自动整理（去重）</CardHeader>
        <CardBody className="space-y-3">
          <div className="grid gap-3 md:grid-cols-2">
            <Input
              label="会话ID（可选，高级）"
              value={compactConversationId}
              onValueChange={setCompactConversationId}
            />
            <Input
              label="QQ号（可选）"
              value={compactUserId}
              onValueChange={setCompactUserId}
            />
            <Select
              label="role（可选）"
              selectedKeys={compactRole ? [compactRole] : []}
              onSelectionChange={(keys) => setCompactRole(String(Array.from(keys)[0] || ""))}
            >
              <SelectItem key="">全部</SelectItem>
              <SelectItem key="user">user</SelectItem>
              <SelectItem key="assistant">assistant</SelectItem>
              <SelectItem key="system">system</SelectItem>
            </Select>
            <Input
              label="每组保留最新N条"
              value={compactKeepLatest}
              onValueChange={setCompactKeepLatest}
            />
            <Input
              className="md:col-span-2"
              label="执行备注 note（执行整理必填）"
              value={compactNote}
              onValueChange={setCompactNote}
            />
          </div>
          <div className="flex gap-2">
            <Button variant="flat" isLoading={compactLoading} onPress={() => runCompact(true)}>
              预览整理
            </Button>
            <Button color="primary" isLoading={compactLoading} onPress={() => runCompact(false)}>
              执行整理
            </Button>
          </div>
          {compactResult && (
            <div className="text-sm rounded-lg border border-default-300/50 p-3 bg-content2/40">
              <p>扫描: {compactResult.scanned} 条</p>
              <p>重复: {compactResult.duplicates} 条</p>
              <p>保留策略: 每组最新 {compactResult.keep_latest} 条</p>
              {compactResult.deleted_ids.length > 0 && (
                <p className="break-words">涉及记录ID: {compactResult.deleted_ids.join(", ")}</p>
              )}
            </div>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader className="pb-1">审计日志（增删改均有备注）</CardHeader>
        <CardBody>
          {auditLoading ? (
            <div className="py-6 flex justify-center"><Spinner /></div>
          ) : (
            <div className="overflow-auto border border-default-300/50 rounded-xl">
              <table className="min-w-full text-sm">
                <thead className="bg-content2/60">
                  <tr>
                    <th className="text-left px-3 py-2">ID</th>
                    <th className="text-left px-3 py-2">记录ID</th>
                    <th className="text-left px-3 py-2">动作</th>
                    <th className="text-left px-3 py-2">操作者</th>
                    <th className="text-left px-3 py-2">备注</th>
                    <th className="text-left px-3 py-2">时间</th>
                  </tr>
                </thead>
                <tbody>
                  {audits.map((row) => (
                    <tr key={row.id} className="border-t border-default-200/40">
                      <td className="px-3 py-2">#{row.id}</td>
                      <td className="px-3 py-2">{row.record_id ?? "-"}</td>
                      <td className="px-3 py-2">{row.action}</td>
                      <td className="px-3 py-2">{row.actor}</td>
                      <td className="px-3 py-2 max-w-[380px] break-words">{row.note || "-"}</td>
                      <td className="px-3 py-2 whitespace-nowrap">{fmtTime(row.created_at)}</td>
                    </tr>
                  ))}
                  {audits.length === 0 && (
                    <tr><td colSpan={6} className="px-3 py-8 text-center text-default-500">暂无审计数据</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>
    </section>
  );
}
