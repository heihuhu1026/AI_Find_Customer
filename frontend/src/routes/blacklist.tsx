import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, BlacklistEntry } from "@/api/client";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Sheet } from "@/components/ui/sheet";
import {
  Ban, Plus, Search, Trash2, Pencil, Download, Upload, FileSpreadsheet,
  Loader2, Tags, AlertTriangle, CheckCircle2,
} from "lucide-react";

function formatTime(iso: string) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return "";
  }
}

type EditorState = { open: boolean; entry: BlacklistEntry | null };

export function BlacklistPage() {
  const queryClient = useQueryClient();
  const [q, setQ] = useState("");
  const [tag, setTag] = useState("");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [editor, setEditor] = useState<EditorState>({ open: false, entry: null });
  const [batchTag, setBatchTag] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["blacklist", q, tag, page],
    queryFn: () => api.listBlacklist({ q, tag, page, page_size: 50 }),
  });

  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / 50));

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["blacklist"] });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteBlacklist(id),
    onSuccess: invalidate,
  });

  const batchMutation = useMutation({
    mutationFn: (payload: { action: "delete" | "tag"; ids: number[]; tag?: string }) =>
      api.batchBlacklist(payload),
    onSuccess: () => {
      setSelected(new Set());
      setBatchTag("");
      invalidate();
    },
  });

  const importMutation = useMutation({
    mutationFn: (file: File) => api.importBlacklist(file),
    onSuccess: (res) => {
      invalidate();
      alert(
        `导入完成：新增 ${res.imported}，更新 ${res.updated}，跳过 ${res.skipped}` +
        (res.errors.length ? `\n\n${res.errors.slice(0, 10).map((e) => `第${e.row}行: ${e.error}`).join("\n")}` : "")
      );
    },
    onError: (e: Error) => alert(`导入失败：${e.message}`),
  });

  const toggleSelect = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const allSelected = items.length > 0 && items.every((i) => selected.has(i.id));

  const toggleAll = () => {
    setSelected((prev) => {
      if (items.every((i) => prev.has(i.id))) {
        const next = new Set(prev);
        items.forEach((i) => next.delete(i.id));
        return next;
      }
      const next = new Set(prev);
      items.forEach((i) => next.add(i.id));
      return next;
    });
  };

  const onFilePicked = (file: File | null) => {
    if (file) importMutation.mutate(file);
    if (fileRef.current) fileRef.current.value = "";
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-3xl font-bold tracking-tight flex items-center gap-2">
            <Ban className="h-7 w-7 text-destructive" />
            客户黑名单
          </h1>
          <p className="text-muted-foreground mt-1">
            加入黑名单的客户（按官网域名）将在搜索、抓取、线索提取各阶段被自动排除，跨所有任务生效。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => window.open(api.blacklistDownloadUrl("template"), "_blank")}>
            <FileSpreadsheet className="h-4 w-4 mr-2" /> 下载模板
          </Button>
          <Button variant="outline" onClick={() => window.open(api.blacklistDownloadUrl("export"), "_blank")}>
            <Download className="h-4 w-4 mr-2" /> 导出 CSV
          </Button>
          <Button variant="outline" onClick={() => fileRef.current?.click()}>
            {importMutation.isPending ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Upload className="h-4 w-4 mr-2" />}
            导入 CSV/Excel
          </Button>
          <Button onClick={() => setEditor({ open: true, entry: null })}>
            <Plus className="h-4 w-4 mr-2" /> 新增客户
          </Button>
        </div>
      </div>

      <input
        ref={fileRef}
        type="file"
        accept=".csv,.xlsx,.xls"
        className="hidden"
        onChange={(e) => onFilePicked(e.target.files?.[0] ?? null)}
      />

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-56">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            value={q}
            onChange={(e) => { setQ(e.target.value); setPage(1); }}
            placeholder="搜索客户名称或域名…"
            className="pl-9"
          />
        </div>
        <Input
          value={tag}
          onChange={(e) => { setTag(e.target.value); setPage(1); }}
          placeholder="按标签筛选"
          className="w-40"
        />
        {selected.size > 0 && (
          <div className="flex items-center gap-2">
            <Input
              value={batchTag}
              onChange={(e) => setBatchTag(e.target.value)}
              placeholder="批量打标签"
              className="w-36"
            />
            <Button variant="outline" onClick={() => batchMutation.mutate({ action: "tag", ids: [...selected], tag: batchTag })} disabled={!batchTag.trim()}>
              <Tags className="h-4 w-4 mr-1" /> 打标签
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (confirm(`确认删除选中的 ${selected.size} 条黑名单？`)) {
                  batchMutation.mutate({ action: "delete", ids: [...selected] });
                }
              }}
            >
              <Trash2 className="h-4 w-4 mr-1" /> 批量删除
            </Button>
          </div>
        )}
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">黑名单列表</CardTitle>
          <CardDescription>共 {total} 条记录</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          {isLoading ? (
            <div className="flex items-center justify-center py-16 text-muted-foreground">
              <Loader2 className="h-6 w-6 animate-spin mr-2" /> 加载中…
            </div>
          ) : items.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
              <Ban className="h-10 w-10 mb-3" />
              <p className="text-sm">暂无黑名单记录</p>
            </div>
          ) : (
            <>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b text-left text-muted-foreground">
                      <th className="py-2 pr-2 w-8">
                        <input type="checkbox" checked={allSelected} onChange={toggleAll} />
                      </th>
                      <th className="py-2 pr-3">客户名称</th>
                      <th className="py-2 pr-3">官网域名</th>
                      <th className="py-2 pr-3">标签</th>
                      <th className="py-2 pr-3">备注</th>
                      <th className="py-2 pr-3">来源</th>
                      <th className="py-2 pr-3">更新时间</th>
                      <th className="py-2 text-right">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((entry) => (
                      <tr key={entry.id} className="border-b last:border-0 hover:bg-muted/40">
                        <td className="py-2 pr-2">
                          <input type="checkbox" checked={selected.has(entry.id)} onChange={() => toggleSelect(entry.id)} />
                        </td>
                        <td className="py-2 pr-3 font-medium">{entry.customer_name}</td>
                        <td className="py-2 pr-3 font-mono text-xs">{entry.domain}</td>
                        <td className="py-2 pr-3">
                          {entry.tags ? entry.tags.split(",").filter(Boolean).map((t) => (
                            <Badge key={t} variant="secondary" className="mr-1">{t}</Badge>
                          )) : <span className="text-muted-foreground">-</span>}
                        </td>
                        <td className="py-2 pr-3 text-muted-foreground max-w-40 truncate">{entry.note || "-"}</td>
                        <td className="py-2 pr-3 text-muted-foreground">{entry.source === "import" ? "导入" : "手动"}</td>
                        <td className="py-2 pr-3 text-muted-foreground">{formatTime(entry.updated_at)}</td>
                        <td className="py-2 text-right whitespace-nowrap">
                          <button className="p-1.5 hover:text-foreground text-muted-foreground" onClick={() => setEditor({ open: true, entry })}>
                            <Pencil className="h-4 w-4" />
                          </button>
                          <button className="p-1.5 hover:text-destructive text-muted-foreground" onClick={() => { if (confirm(`删除黑名单 ${entry.customer_name}？`)) deleteMutation.mutate(entry.id); }}>
                            <Trash2 className="h-4 w-4" />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {pageCount > 1 && (
                <div className="flex items-center justify-end gap-2 pt-2">
                  <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>上一页</Button>
                  <span className="text-sm text-muted-foreground">{page} / {pageCount}</span>
                  <Button variant="outline" size="sm" disabled={page >= pageCount} onClick={() => setPage((p) => p + 1)}>下一页</Button>
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>

      <EditorSheet
        key={editor.open ? (editor.entry?.id ?? "new") : "closed"}
        editor={editor}
        onClose={() => setEditor({ open: false, entry: null })}
        onSaved={() => { setEditor({ open: false, entry: null }); invalidate(); }}
      />
    </div>
  );
}

function EditorSheet({
  editor,
  onClose,
  onSaved,
}: {
  editor: EditorState;
  onClose: () => void;
  onSaved: () => void;
}) {
  const entry = editor.entry;
  const [name, setName] = useState(entry?.customer_name ?? "");
  const [domain, setDomain] = useState(entry?.domain ?? "");
  const [note, setNote] = useState(entry?.note ?? "");
  const [tags, setTags] = useState(entry?.tags ?? "");
  const [check, setCheck] = useState<{ valid: boolean; exists_domain: boolean; suspected_name: boolean; error: string } | null>(null);
  const [checking, setChecking] = useState(false);

  const runCheck = async () => {
    if (!domain.trim()) { setCheck(null); return; }
    setChecking(true);
    try {
      const res = await api.checkBlacklist(domain.trim(), name.trim());
      setCheck(res);
    } catch {
      setCheck(null);
    } finally {
      setChecking(false);
    }
  };

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (entry) {
        return api.updateBlacklist(entry.id, { customer_name: name, domain, note, tags });
      }
      return api.createBlacklist({ customer_name: name, domain, note, tags });
    },
    onSuccess: onSaved,
    onError: (e: Error) => alert(e.message),
  });

  const isEdit = Boolean(entry);

  return (
    <Sheet open={editor.open} onClose={onClose}>
      <div className="p-6 space-y-5">
        <div>
          <h2 className="text-lg font-semibold">{isEdit ? "编辑黑名单" : "新增黑名单客户"}</h2>
          <p className="text-sm text-muted-foreground mt-1">按官网域名唯一识别；同域名自动合并更新。</p>
        </div>

        <div className="space-y-1.5">
          <Label>客户名称</Label>
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Acme GmbH" />
        </div>

        <div className="space-y-1.5">
          <Label>官网域名 *</Label>
          <Input
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            onBlur={runCheck}
            placeholder="acme.com 或 https://www.acme.com"
            className="font-mono"
          />
          {checking && <p className="text-xs text-muted-foreground">校验中…</p>}
          {check && (
            check.valid ? (
              check.exists_domain ? (
                <p className="text-xs text-amber-600 flex items-center gap-1">
                  <AlertTriangle className="h-3.5 w-3.5" /> 该域名已存在，保存将更新原记录
                </p>
              ) : check.suspected_name ? (
                <p className="text-xs text-amber-600 flex items-center gap-1">
                  <AlertTriangle className="h-3.5 w-3.5" /> 疑似重复：已存在同名客户（不同域名）
                </p>
              ) : (
                <p className="text-xs text-emerald-600 flex items-center gap-1">
                  <CheckCircle2 className="h-3.5 w-3.5" /> 域名可用
                </p>
              )
            ) : (
              <p className="text-xs text-destructive">{check.error}</p>
            )
          )}
        </div>

        <div className="space-y-1.5">
          <Label>标签</Label>
          <Input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="competitor,partner（逗号分隔）" />
        </div>

        <div className="space-y-1.5">
          <Label>备注</Label>
          <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="已合作 / 竞对 / 垃圾站" />
        </div>

        <Separator />

        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose}>取消</Button>
          <Button onClick={() => saveMutation.mutate()} disabled={!domain.trim() || saveMutation.isPending}>
            {saveMutation.isPending ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : null}
            保存
          </Button>
        </div>
      </div>
    </Sheet>
  );
}
