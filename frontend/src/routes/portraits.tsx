import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { api, Portrait } from "@/api/client";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Users, Sparkles, Plus, Trash2, Loader2, Building2, Layers } from "lucide-react";

export function PortraitsPage() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [domainsText, setDomainsText] = useState("");
  const [summary, setSummary] = useState("");
  const [formError, setFormError] = useState("");

  const { data, isLoading } = useQuery({
    queryKey: ["portraits"],
    queryFn: api.listPortraits,
    retry: false,
  });
  const portraits = data?.items ?? [];

  const createMutation = useMutation({
    mutationFn: (payload: { name: string; source_customers: string[]; insight_summary: string }) =>
      api.createPortrait(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["portraits"] });
      setName("");
      setDomainsText("");
      setSummary("");
    },
  });

  const buildMutation = useMutation({
    mutationFn: (payload: { name: string; source_customers: string[]; insight_summary: string }) =>
      api.buildPortrait(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["portraits"] });
      setName("");
      setDomainsText("");
      setSummary("");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (portraitId: string) => api.deletePortrait(portraitId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["portraits"] }),
  });

  const parseDomains = (): string[] =>
    domainsText
      .split(/[\n,，]/)
      .map((d) => d.trim())
      .filter(Boolean);

  const handleCreate = (mode: "manual" | "build") => {
    setFormError("");
    const trimmedName = name.trim();
    const domains = parseDomains();
    if (!trimmedName) {
      setFormError("请填写画像名称");
      return;
    }
    if (mode === "build" && domains.length === 0) {
      setFormError("AI 构建需要至少一个源客户域名");
      return;
    }
    const payload = { name: trimmedName, source_customers: domains, insight_summary: summary.trim() };
    if (mode === "build") buildMutation.mutate(payload);
    else createMutation.mutate(payload);
  };

  const busy = createMutation.isPending || buildMutation.isPending;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">客户画像库</h1>
        <p className="text-muted-foreground mt-1">基于源客户洞察沉淀理想客户画像（ICP），并可直接从画像扩量挖掘线索</p>
      </div>

      {/* 新建画像 */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg flex items-center gap-2">
            <Plus className="h-5 w-5 text-primary" />
            新建画像
          </CardTitle>
          <CardDescription>手动创建（无 AI），或基于源客户域名 AI 聚合 ICP</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 md:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">画像名称</label>
              <Input placeholder="例如：欧洲光伏分销商" value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">源客户域名（逗号或换行分隔）</label>
              <Input placeholder="example1.com, example2.com" value={domainsText} onChange={(e) => setDomainsText(e.target.value)} />
            </div>
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium">洞察摘要（可选）</label>
            <Input placeholder="一句话描述该画像的核心特征" value={summary} onChange={(e) => setSummary(e.target.value)} />
          </div>
          {formError && <p className="text-sm text-destructive">{formError}</p>}
          <div className="flex flex-wrap gap-2">
            <Button onClick={() => handleCreate("manual")} disabled={busy} variant="outline">
              {createMutation.isPending ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Plus className="h-4 w-4 mr-2" />}
              手动创建
            </Button>
            <Button onClick={() => handleCreate("build")} disabled={busy}>
              {buildMutation.isPending ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Sparkles className="h-4 w-4 mr-2" />}
              AI 构建（消耗额度）
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* 画像列表 */}
      {isLoading ? (
        <Card className="border-dashed">
          <CardContent className="flex items-center justify-center gap-3 py-16 text-muted-foreground">
            <Loader2 className="h-6 w-6 animate-spin" />
            <span>正在加载画像…</span>
          </CardContent>
        </Card>
      ) : portraits.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-16">
            <Layers className="h-12 w-12 text-muted-foreground mb-4" />
            <h3 className="text-lg font-semibold mb-2">还没有客户画像</h3>
            <p className="text-muted-foreground mb-6">从上方新建，或基于源客户域名 AI 构建第一个画像</p>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {portraits.map((p: Portrait) => {
            const icp = p.icp ?? {};
            const tagCount = (icp.industries?.length ?? 0) + (icp.regions?.length ?? 0) + (icp.keywords?.length ?? 0);
            return (
              <Card key={p.id} className="hover:shadow-md transition-shadow">
                <CardHeader className="pb-2">
                  <div className="flex items-start justify-between gap-2">
                    <CardTitle className="text-base">
                      <Link to="/portraits/$portraitId" params={{ portraitId: p.id }} className="hover:underline">
                        {p.name}
                      </Link>
                    </CardTitle>
                    <button
                      type="button"
                      className="text-muted-foreground hover:text-destructive"
                      title="删除画像"
                      disabled={deleteMutation.isPending}
                      onClick={() => deleteMutation.mutate(p.id)}
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                  {p.insight_summary && (
                    <CardDescription className="line-clamp-2">{p.insight_summary}</CardDescription>
                  )}
                </CardHeader>
                <CardContent className="space-y-3">
                  <div className="flex items-center gap-4 text-sm text-muted-foreground">
                    <span className="inline-flex items-center gap-1.5">
                      <Building2 className="h-3.5 w-3.5" />
                      {p.source_customers?.length ?? 0} 源客户
                    </span>
                    <span className="inline-flex items-center gap-1.5">
                      <Users className="h-3.5 w-3.5" />
                      {p.total_leads ?? 0} 线索
                    </span>
                  </div>
                  {tagCount > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {(icp.industries ?? []).slice(0, 3).map((x) => (
                        <Badge key={x} variant="secondary" className="text-[10px] px-1.5 py-0 h-5">{x}</Badge>
                      ))}
                      {(icp.regions ?? []).slice(0, 3).map((x) => (
                        <Badge key={x} variant="outline" className="text-[10px] px-1.5 py-0 h-5">{x}</Badge>
                      ))}
                    </div>
                  )}
                  <Link to="/portraits/$portraitId" params={{ portraitId: p.id }} className="text-primary text-sm hover:underline">
                    查看详情
                  </Link>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
