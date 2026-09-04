import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { api, HuntListItem } from "@/api/client";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Crosshair, Loader2, Globe, MapPin, Tag, Users, Mail, ArrowRight } from "lucide-react";

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

function statusMeta(status: string) {
  switch (status) {
    case "completed": return { label: "已完成", variant: "success" as const };
    case "running": return { label: "进行中", variant: "warning" as const };
    case "failed": return { label: "失败", variant: "destructive" as const };
    case "cancelled": return { label: "已取消", variant: "secondary" as const };
    case "pending": return { label: "待处理", variant: "secondary" as const };
    default: return { label: status || "未知", variant: "secondary" as const };
  }
}

function huntTitle(hunt: HuntListItem) {
  if (hunt.website_url) {
    try {
      return new URL(hunt.website_url).hostname.replace(/^www\./, "");
    } catch { /* fall through */ }
  }
  if (hunt.product_keywords?.length) {
    return hunt.product_keywords.slice(0, 2).join(", ");
  }
  return hunt.hunt_id.slice(0, 8);
}

export function HuntsListPage() {
  const { data: hunts, isLoading, isFetching, error } = useQuery({
    queryKey: ["hunts"],
    queryFn: api.listHunts,
    refetchInterval: 5000,
  });

  const list = hunts ?? [];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight flex items-center gap-2">
            <Crosshair className="h-7 w-7 text-primary" />
            Hunt 列表
          </h1>
          <p className="text-muted-foreground mt-1">
            查看所有 Hunt 执行记录，包括直接执行、无队列任务关联的历史 Hunt。
          </p>
        </div>
        <Link to="/hunts/new" search={{ fromJob: "" }}>
          <Button>新建任务</Button>
        </Link>
      </div>

      {error && (
        <Card className="border-amber-300">
          <CardContent className="py-4 text-sm text-amber-700">{error.message}</CardContent>
        </Card>
      )}

      {isLoading ? (
        <Card className="border-dashed">
          <CardContent className="flex items-center justify-center gap-3 py-16 text-muted-foreground">
            <Loader2 className="h-6 w-6 animate-spin" />
            <span>加载 Hunt 列表…</span>
          </CardContent>
        </Card>
      ) : list.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-16 text-muted-foreground">
            <Crosshair className="h-12 w-12 mb-4" />
            <h3 className="text-lg font-semibold mb-2">还没有 Hunt 记录</h3>
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="flex items-center justify-between text-sm text-muted-foreground">
            <span>共 {list.length} 个 Hunt</span>
            {isFetching && <span className="inline-flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin" /> 刷新中</span>}
          </div>
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {list.map((hunt) => {
              const meta = statusMeta(hunt.status);
              return (
                <Card key={hunt.hunt_id} className="hover:shadow-md transition-shadow">
                  <CardHeader className="flex flex-row items-start justify-between space-y-0 pb-2">
                    <CardTitle className="text-sm font-medium truncate max-w-[70%]">
                      {huntTitle(hunt)}
                    </CardTitle>
                    <Badge variant={meta.variant}>{meta.label}</Badge>
                  </CardHeader>
                  <CardContent className="space-y-3 text-sm">
                    <div className="flex items-center gap-4">
                      <div className="flex items-center gap-1.5 text-2xl font-bold">
                        <Users className="h-5 w-5 text-muted-foreground" />
                        {hunt.leads_count}
                        <span className="text-sm font-normal text-muted-foreground">线索</span>
                      </div>
                      <div className="flex items-center gap-1.5 text-muted-foreground">
                        <Mail className="h-4 w-4" />
                        {hunt.email_sequences_count} 序列
                      </div>
                    </div>
                    <div className="space-y-1.5 text-xs text-muted-foreground">
                      {hunt.website_url && (
                        <div className="flex items-center gap-1.5 truncate">
                          <Globe className="h-3 w-3 shrink-0" />
                          <span className="truncate">{hunt.website_url}</span>
                        </div>
                      )}
                      {hunt.product_keywords?.length > 0 && (
                        <div className="flex items-center gap-1.5 truncate">
                          <Tag className="h-3 w-3 shrink-0" />
                          <span className="truncate">{hunt.product_keywords.join(", ")}</span>
                        </div>
                      )}
                      {hunt.target_regions?.length > 0 && (
                        <div className="flex items-center gap-1.5 truncate">
                          <MapPin className="h-3 w-3 shrink-0" />
                          <span className="truncate">{hunt.target_regions.join(", ")}</span>
                        </div>
                      )}
                      {hunt.created_at && (
                        <div className="flex items-center gap-1.5">
                          <span>{formatTime(hunt.created_at)}</span>
                          {hunt.hunt_round > 0 && <span>· 第 {hunt.hunt_round} 轮</span>}
                        </div>
                      )}
                    </div>
                    <Link to="/hunts/$huntId" params={{ huntId: hunt.hunt_id }} className="inline-flex items-center gap-1 text-primary hover:underline text-sm">
                      查看详情 <ArrowRight className="h-3.5 w-3.5" />
                    </Link>
                  </CardContent>
                </Card>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
