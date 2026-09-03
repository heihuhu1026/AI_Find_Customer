import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useParams, useNavigate } from "@tanstack/react-router";
import { api, ICPProfile } from "@/api/client";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ArrowLeft, Loader2, Sparkles, Building2, Layers } from "lucide-react";

function Section({ title, items, fallback }: { title: string; items: string[]; fallback: string }) {
  return (
    <div>
      <h3 className="text-sm font-semibold text-muted-foreground mb-2">{title}</h3>
      {items.length > 0 ? (
        <div className="flex flex-wrap gap-1.5">
          {items.map((x) => (
            <Badge key={x} variant="secondary">{x}</Badge>
          ))}
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">{fallback}</p>
      )}
    </div>
  );
}

export function PortraitDetailPage() {
  const { portraitId } = useParams({ from: "/portraits/$portraitId" });
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [expandError, setExpandError] = useState("");

  const { data: portrait, isLoading, error } = useQuery({
    queryKey: ["portrait", portraitId],
    queryFn: () => api.getPortrait(portraitId),
    retry: false,
  });

  const expandMutation = useMutation({
    mutationFn: () => api.expandPortrait(portraitId, 1, 1),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["portraits"] });
      if (data?.hunt_id) {
        navigate({ to: "/hunts/$huntId", params: { huntId: data.hunt_id } });
      }
    },
    onError: (err: unknown) => {
      setExpandError(err instanceof Error ? err.message : "扩量失败");
    },
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-24 text-muted-foreground">
        <Loader2 className="h-6 w-6 animate-spin mr-3" />
        正在加载画像…
      </div>
    );
  }

  if (error || !portrait) {
    return (
      <div className="space-y-4">
        <Link to="/portraits" className="inline-flex items-center gap-1.5 text-sm text-primary hover:underline">
          <ArrowLeft className="h-4 w-4" /> 返回画像库
        </Link>
        <Card className="border-dashed">
          <CardContent className="py-16 text-center text-muted-foreground">
            画像不存在或加载失败
          </CardContent>
        </Card>
      </div>
    );
  }

  const icp: ICPProfile = portrait.icp ?? {};

  return (
    <div className="space-y-6">
      <Link to="/portraits" className="inline-flex items-center gap-1.5 text-sm text-primary hover:underline">
        <ArrowLeft className="h-4 w-4" /> 返回画像库
      </Link>

      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">{portrait.name}</h1>
          {portrait.insight_summary && (
            <p className="text-muted-foreground mt-1">{portrait.insight_summary}</p>
          )}
        </div>
        <Button onClick={() => expandMutation.mutate()} disabled={expandMutation.isPending}>
          {expandMutation.isPending ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Sparkles className="h-4 w-4 mr-2" />}
          画像扩量（最小配置）
        </Button>
      </div>

      {expandError && <p className="text-sm text-destructive">{expandError}</p>}

      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">源客户</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">{portrait.source_customers?.length ?? 0}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">已发起 Hunt</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">{portrait.hunt_count ?? 0}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">累计线索</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-semibold">{portrait.total_leads ?? 0}</div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg flex items-center gap-2">
            <Layers className="h-5 w-5 text-primary" />
            理想客户画像（ICP）
          </CardTitle>
          <CardDescription>用于竞品反向挖掘与相似买家扩量的画像特征</CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <Section title="行业" items={icp.industries ?? []} fallback="暂无行业标签" />
          <Section title="目标地区" items={icp.regions ?? []} fallback="暂无地区标签" />
          <Section title="产品关键词" items={icp.keywords ?? []} fallback="暂无关键词" />
          <Section title="技术栈" items={icp.tech_stack ?? []} fallback="暂无技术栈标签" />
          <Section title="企业规模" items={icp.employee_range ?? []} fallback="暂无规模标签" />
        </CardContent>
      </Card>

      {portrait.source_customers?.length ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-lg flex items-center gap-2">
              <Building2 className="h-5 w-5 text-primary" />
              源客户域名
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-2">
              {portrait.source_customers.map((d) => (
                <Badge key={d} variant="outline">{d}</Badge>
              ))}
            </div>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}
