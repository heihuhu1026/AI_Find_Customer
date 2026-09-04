import { createRootRoute, createRoute } from "@tanstack/react-router";
import { RootLayout } from "./root";
import { DashboardPage } from "./dashboard";
import { NewHuntPage } from "./new-hunt";
import { HuntDetailPage } from "./hunt-detail";
import { HuntsListPage } from "./hunts-list";
import { AutomationJobPage } from "./automation-job";
import { BlacklistPage } from "./blacklist";
import { SettingsPage } from "./settings";

const rootRoute = createRootRoute({ component: RootLayout });

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: DashboardPage,
});

const newHuntRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/hunts/new",
  validateSearch: (search: Record<string, unknown>) => ({
    fromJob: typeof search.fromJob === "string" ? search.fromJob : "",
  }),
  component: NewHuntPage,
});

const huntsListRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/hunts",
  component: HuntsListPage,
});

const huntDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/hunts/$huntId",
  component: HuntDetailPage,
});

const automationJobRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/automation/$jobId",
  component: AutomationJobPage,
});

const blacklistRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/blacklist",
  component: BlacklistPage,
});

const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: SettingsPage,
});

export const routeTree = rootRoute.addChildren([
  indexRoute,
  newHuntRoute,
  huntsListRoute,
  huntDetailRoute,
  automationJobRoute,
  blacklistRoute,
  settingsRoute,
]);
