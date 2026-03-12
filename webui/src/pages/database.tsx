import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent } from "react";
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  Chip,
  Input,
  Select,
  SelectItem,
  Spinner,
  Switch,
} from "@heroui/react";
import { Database, Download, RefreshCw, Search, Upload } from "lucide-react";
import {
  api,
  DbOverviewItem,
  DbRowsResponse,
  DbTableInfo,
} from "../api/client";
import { NotificationContainer } from "../components/notification";
import { useNotifications } from "../hooks/useNotifications";

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let n = value;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatTime(ts: number): string {
  if (!Number.isFinite(ts) || ts <= 0) return "-";
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}

function renderCell(value: unknown): string {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function resolveDownloadFilename(contentDisposition: string | null, fallback: string): string {
  const raw = contentDisposition || "";
  const utf8Match = raw.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match?.[1]) {
    return decodeURIComponent(utf8Match[1]);
  }
  const basicMatch = raw.match(/filename="?([^";]+)"?/i);
  if (basicMatch?.[1]) {
    return basicMatch[1];
  }
  return fallback;
}

export default function DatabasePage() {
  const { notifications, confirm, success, danger } = useNotifications();
  const [overview, setOverview] = useState<DbOverviewItem[]>([]);
  const [selectedDb, setSelectedDb] = useState("knowledge");
  const [tables, setTables] = useState<DbTableInfo[]>([]);
  const [selectedTable, setSelectedTable] = useState("");
  const [includeSystem, setIncludeSystem] = useState(false);
  const [loadingOverview, setLoadingOverview] = useState(true);
  const [loadingTables, setLoadingTables] = useState(false);
  const [loadingRows, setLoadingRows] = useState(false);
  const [clearingTable, setClearingTable] = useState(false);
  const [exportingDb, setExportingDb] = useState(false);
  const [importingDb, setImportingDb] = useState(false);
  const [error, setError] = useState("");
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const [queryInput, setQueryInput] = useState("");
  const [executedQuery, setExecutedQuery] = useState("");
  const [queryRefreshToken, setQueryRefreshToken] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [rowsData, setRowsData] = useState<DbRowsResponse | null>(null);

  const loadOverview = useCallback(async () => {
    setLoadingOverview(true);
    try {
      const res = await api.getDbOverview();
      const dbs = res.databases || [];
      setOverview(dbs);
      if (dbs.length > 0 && !dbs.find((d) => d.name === selectedDb)) {
        setSelectedDb(dbs[0].name);
      }
      setError("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "读取数据库概览失败");
    } finally {
      setLoadingOverview(false);
    }
  }, [selectedDb]);

  const loadTables = useCallback(async () => {
    if (!selectedDb) return;
    setLoadingTables(true);
    try {
      const res = await api.getDbTables(selectedDb, includeSystem, false);
      const next = res.tables || [];
      setTables(next);
      if (!next.find((t) => t.name === selectedTable)) {
        setSelectedTable(next.length > 0 ? next[0].name : "");
      }
      setError("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "读取数据表失败");
      setTables([]);
      setSelectedTable("");
    } finally {
      setLoadingTables(false);
    }
  }, [includeSystem, selectedDb, selectedTable]);

  const loadRows = useCallback(async () => {
    if (!selectedDb || !selectedTable) {
      setRowsData(null);
      return;
    }
    setLoadingRows(true);
    try {
      const res = await api.getDbRows(selectedDb, {
        table: selectedTable,
        page,
        pageSize,
        query: executedQuery,
        includeSystem,
      });
      setRowsData(res);
      setError("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "读取行数据失败");
      setRowsData(null);
    } finally {
      setLoadingRows(false);
    }
  }, [executedQuery, includeSystem, page, pageSize, queryRefreshToken, selectedDb, selectedTable]);

  useEffect(() => {
    loadOverview();
  }, [loadOverview]);

  useEffect(() => {
    loadTables();
    setPage(1);
  }, [loadTables]);

  useEffect(() => {
    loadRows();
  }, [loadRows]);

  const total = rowsData?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const currentDb = useMemo(
    () => overview.find((db) => db.name === selectedDb) || null,
    [overview, selectedDb],
  );
  const applyQuery = useCallback(() => {
    setExecutedQuery(queryInput.trim());
    setPage(1);
    setQueryRefreshToken((v) => v + 1);
  }, [queryInput]);
  const clearQuery = useCallback(() => {
    setQueryInput("");
    setExecutedQuery("");
    setPage(1);
    setQueryRefreshToken((v) => v + 1);
  }, []);
  const clearCurrentTable = useCallback(async () => {
    if (!selectedDb || !selectedTable || clearingTable) return;

    confirm(
      "危险操作：清空数据表",
      `你即将清空 ${selectedDb}.${selectedTable} 的全部数据。这是一个删库操作，此操作不可撤销！你真的要执行吗？`,
      async () => {
        setClearingTable(true);
        try {
          const res = await api.clearDbTable(selectedDb, selectedTable);
          success("清空成功", `共删除 ${res.deleted} 行数据`);
          setPage(1);
          setQueryInput("");
          setExecutedQuery("");
          setQueryRefreshToken((v) => v + 1);
          await loadOverview();
          await loadTables();
          await loadRows();
        } catch (e: unknown) {
          danger("清空失败", e instanceof Error ? e.message : "清空数据失败");
        } finally {
          setClearingTable(false);
        }
      },
      {
        type: "danger",
        confirmText: "确认删除",
        cancelText: "取消",
      }
    );
  }, [clearingTable, loadOverview, loadRows, loadTables, selectedDb, selectedTable, confirm, success, danger]);

  const exportCurrentDb = useCallback(async () => {
    if (!selectedDb || exportingDb) return;
    setExportingDb(true);
    try {
      const { blob, response } = await api.exportDb(selectedDb);
      const filename = resolveDownloadFilename(
        response.headers.get("Content-Disposition"),
        `${selectedDb}.db`,
      );
      const blobUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = blobUrl;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(blobUrl);
      success("导出成功", `已下载数据库备份：${filename}`);
    } catch (e: unknown) {
      danger("导出失败", e instanceof Error ? e.message : "数据库导出失败");
    } finally {
      setExportingDb(false);
    }
  }, [danger, exportingDb, selectedDb, success]);

  const importDbFile = useCallback(async (file: File) => {
    if (!selectedDb || importingDb) return;
    setImportingDb(true);
    try {
      const res = await api.importDb(selectedDb, file);
      success(
        "导入成功",
        `${res.message}。已自动备份原库到 ${res.backup_path}${res.restart_recommended ? "，建议随后重启服务让长连接模块完全刷新。" : ""}`,
        6000,
      );
      setPage(1);
      setQueryInput("");
      setExecutedQuery("");
      setQueryRefreshToken((v) => v + 1);
      await loadOverview();
      await loadTables();
      await loadRows();
    } catch (e: unknown) {
      danger("导入失败", e instanceof Error ? e.message : "数据库导入失败");
    } finally {
      setImportingDb(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    }
  }, [danger, importingDb, loadOverview, loadRows, loadTables, selectedDb, success]);

  const chooseImportFile = useCallback(() => {
    fileInputRef.current?.click();
  }, []);

  const onImportFileChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file || !selectedDb) {
      return;
    }
    confirm(
      "确认导入数据库",
      `即将用 ${file.name} 覆盖导入 ${selectedDb}。系统会先自动备份当前数据库，但导入后建议手动重启服务。确认继续吗？`,
      () => {
        void importDbFile(file);
      },
      {
        type: "warning",
        confirmText: "确认导入",
        cancelText: "取消",
      },
    );
  }, [confirm, importDbFile, selectedDb]);

  return (
    <>
      <NotificationContainer notifications={notifications} />
      <input
        ref={fileInputRef}
        type="file"
        accept=".db,.sqlite,.sqlite3"
        className="hidden"
        onChange={onImportFileChange}
      />
      <section className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Database size={20} />
          <h2 className="text-xl font-bold">数据库查看</h2>
          <Chip size="sm" variant="flat" color={selectedTable ? "warning" : "primary"}>
            {selectedTable ? "可清空当前表" : "只读"}
          </Chip>
        </div>
        <div className="flex items-center gap-2">
          {selectedTable && (
            <Button
              color="danger"
              variant="flat"
              onPress={clearCurrentTable}
              isLoading={clearingTable}
              isDisabled={loadingRows || loadingTables}
            >
              清空当前表
            </Button>
          )}
          <Button
            variant="flat"
            startContent={<Upload size={16} />}
            onPress={chooseImportFile}
            isDisabled={!selectedDb || loadingOverview}
            isLoading={importingDb}
          >
            导入当前库
          </Button>
          <Button
            variant="flat"
            startContent={<Download size={16} />}
            onPress={exportCurrentDb}
            isDisabled={!selectedDb || loadingOverview}
            isLoading={exportingDb}
          >
            导出当前库
          </Button>
          <Button
            variant="flat"
            startContent={<RefreshCw size={16} />}
            onPress={() => {
              loadOverview();
              loadTables();
              loadRows();
            }}
          >
            刷新
          </Button>
        </div>
      </div>

      {error && (
        <p className={`${error.startsWith("[OK]") ? "text-success" : "text-danger"} text-sm`}>
          {error.replace(/^\[OK\]\s*/, "")}
        </p>
      )}

      <Card>
        <CardHeader className="pb-1">数据源</CardHeader>
        <CardBody className="grid gap-3 md:grid-cols-4">
          <Select
            label="数据库"
            selectedKeys={selectedDb ? [selectedDb] : []}
            onSelectionChange={(keys) => {
              const val = Array.from(keys)[0];
              setSelectedDb(val ? String(val) : "");
              setPage(1);
            }}
            isDisabled={loadingOverview}
          >
            {overview.map((db) => (
              <SelectItem key={db.name}>{db.name}</SelectItem>
            ))}
          </Select>
          <Select
            label="数据表"
            selectedKeys={selectedTable ? [selectedTable] : []}
            onSelectionChange={(keys) => {
              const val = Array.from(keys)[0];
              setSelectedTable(val ? String(val) : "");
              setPage(1);
            }}
            isDisabled={loadingTables || tables.length === 0}
          >
            {tables.map((t) => (
              <SelectItem key={t.name}>{t.name}</SelectItem>
            ))}
          </Select>
          <Select
            label="每页行数"
            selectedKeys={[String(pageSize)]}
            onSelectionChange={(keys) => {
              const val = Number(Array.from(keys)[0] || 50);
              setPageSize(Number.isFinite(val) ? val : 50);
              setPage(1);
            }}
          >
            {[20, 50, 100, 200].map((n) => (
              <SelectItem key={String(n)}>{String(n)}</SelectItem>
            ))}
          </Select>
          <div className="flex items-end pb-2">
            <Switch isSelected={includeSystem} onValueChange={(v) => setIncludeSystem(v)}>
              显示系统表
            </Switch>
          </div>
        </CardBody>
      </Card>

      <Card>
        <CardHeader className="pb-1">库维护</CardHeader>
        <CardBody className="grid gap-3 md:grid-cols-2">
          <div className="rounded-2xl border border-default-200/60 bg-content2/40 p-4">
            <p className="text-sm font-semibold">当前数据库</p>
            <p className="mt-2 text-xs text-default-500 break-all">{currentDb?.path || "未选择"}</p>
            <p className="mt-3 text-xs text-default-500">
              导出会直接下载当前库文件；导入会先备份原库，再覆盖写入新数据库。
            </p>
          </div>
          <div className="rounded-2xl border border-warning/40 bg-warning/5 p-4">
            <p className="text-sm font-semibold text-warning-700">导入提醒</p>
            <p className="mt-2 text-xs text-default-600">
              导入完成后页面会自动刷新，但如果有长期持有数据库连接的模块，仍建议你手动重启服务，确保运行时完全切到新数据。
            </p>
          </div>
        </CardBody>
      </Card>

      <Card>
        <CardHeader className="pb-1">过滤</CardHeader>
        <CardBody className="flex flex-col gap-3 md:flex-row">
          <Input
            label="关键词搜索"
            value={queryInput}
            isClearable
            onClear={clearQuery}
            onValueChange={setQueryInput}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                applyQuery();
              }
            }}
            placeholder="按当前表全部列模糊匹配"
            startContent={<Search size={16} />}
          />
          <div className="flex items-end gap-2 pb-1">
            <Button color="primary" onPress={applyQuery}>
              查询
            </Button>
            <Button variant="flat" onPress={clearQuery}>
              清空筛选
            </Button>
          </div>
        </CardBody>
      </Card>

      <Card>
        <CardHeader className="flex items-center justify-between pb-1">
          <div className="text-sm">
            {currentDb ? `${currentDb.name} · ${selectedTable || "-"} · 共 ${total} 行` : "无数据库"}
          </div>
          <div className="text-xs text-default-500">
            {currentDb ? `${formatBytes(currentDb.size_bytes)} · ${formatTime(currentDb.modified_at)}` : ""}
          </div>
        </CardHeader>
        <CardBody className="space-y-3">
          <div className="flex items-center justify-between">
            <div className="text-xs text-default-500">
              第 {rowsData?.page ?? page} / {totalPages} 页
            </div>
            <div className="flex gap-2">
              <Button
                size="sm"
                variant="flat"
                isDisabled={page <= 1 || loadingRows}
                onPress={() => setPage((p) => Math.max(1, p - 1))}
              >
                上一页
              </Button>
              <Button
                size="sm"
                variant="flat"
                isDisabled={page >= totalPages || loadingRows}
                onPress={() => setPage((p) => Math.min(totalPages, p + 1))}
              >
                下一页
              </Button>
            </div>
          </div>

          {loadingRows ? (
            <div className="py-12 flex justify-center">
              <Spinner />
            </div>
          ) : (
            <div className="overflow-auto border border-default-300/50 rounded-xl">
              <table className="min-w-full text-sm">
                <thead className="bg-content2/60">
                  <tr>
                    {(rowsData?.columns || []).map((col) => (
                      <th key={col.name} className="text-left px-3 py-2 font-semibold whitespace-nowrap">
                        {col.name}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {(rowsData?.rows || []).map((row, idx) => (
                    <tr key={idx} className="border-t border-default-200/40">
                      {(rowsData?.columns || []).map((col) => (
                        <td key={col.name} className="px-3 py-2 align-top">
                          <div className="max-w-[440px] break-words">
                            {renderCell(row[col.name])}
                          </div>
                        </td>
                      ))}
                    </tr>
                  ))}
                  {(rowsData?.rows || []).length === 0 && (
                    <tr>
                      <td
                        colSpan={(rowsData?.columns || []).length || 1}
                        className="px-3 py-8 text-center text-default-500"
                      >
                        当前条件下没有数据
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </CardBody>
      </Card>
    </section>
    </>
  );
}
