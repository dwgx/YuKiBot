import { useCallback, useEffect, useMemo, useState } from "react";
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
import { Database, RefreshCw, Search } from "lucide-react";
import {
  api,
  DbOverviewItem,
  DbRowsResponse,
  DbTableInfo,
} from "../api/client";

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

export default function DatabasePage() {
  const [overview, setOverview] = useState<DbOverviewItem[]>([]);
  const [selectedDb, setSelectedDb] = useState("knowledge");
  const [tables, setTables] = useState<DbTableInfo[]>([]);
  const [selectedTable, setSelectedTable] = useState("");
  const [includeSystem, setIncludeSystem] = useState(false);
  const [loadingOverview, setLoadingOverview] = useState(true);
  const [loadingTables, setLoadingTables] = useState(false);
  const [loadingRows, setLoadingRows] = useState(false);
  const [error, setError] = useState("");

  const [queryInput, setQueryInput] = useState("");
  const [executedQuery, setExecutedQuery] = useState("");
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
  }, [executedQuery, includeSystem, page, pageSize, selectedDb, selectedTable]);

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

  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Database size={20} />
          <h2 className="text-xl font-bold">数据库查看</h2>
          <Chip size="sm" variant="flat" color="primary">只读</Chip>
        </div>
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

      {error && <p className="text-danger text-sm">{error}</p>}

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
        <CardHeader className="pb-1">过滤</CardHeader>
        <CardBody className="flex flex-col gap-3 md:flex-row">
          <Input
            label="关键词搜索"
            value={queryInput}
            onValueChange={setQueryInput}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                setExecutedQuery(queryInput.trim());
                setPage(1);
              }
            }}
            placeholder="按当前表全部列模糊匹配"
            startContent={<Search size={16} />}
          />
          <div className="flex items-end gap-2 pb-1">
            <Button
              color="primary"
              onPress={() => {
                setExecutedQuery(queryInput.trim());
                setPage(1);
              }}
            >
              查询
            </Button>
            <Button
              variant="flat"
              onPress={() => {
                setQueryInput("");
                setExecutedQuery("");
                setPage(1);
              }}
            >
              清空
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
  );
}

