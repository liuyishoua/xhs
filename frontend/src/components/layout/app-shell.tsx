import {
  AimOutlined,
  AppstoreOutlined,
  BarChartOutlined,
  BellOutlined,
  MessageOutlined,
  CloudDownloadOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  FileTextOutlined,
  KeyOutlined,
  LogoutOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  MoonOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  ScheduleOutlined,
  SearchOutlined,
  SendOutlined,
  ThunderboltOutlined,
  StarOutlined,
  SunOutlined,
  UserOutlined,
  VideoCameraOutlined,
} from "@ant-design/icons";
import {
  Avatar,
  Badge,
  Button,
  Col,
  Dropdown,
  Drawer,
  Grid,
  Layout,
  List,
  Menu,
  Row,
  Space,
  Spin,
  Tag,
  Typography,
} from "antd";
import type { MenuProps } from "antd";
import { Suspense, startTransition, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { UNSAFE_LocationContext, useLocation, useNavigate } from "react-router-dom";
import { useKeepAliveContext, useKeepAliveRef } from 'keepalive-for-react';
import KeepAliveRouteOutlet from "keepalive-for-react-router";

import { preloadRoute } from "../../app/lazy-pages";
import { useAuth } from "../../hooks/use-auth";
import { useThemeMode } from "../../app/providers";
import { fetchNotifications, markAllNotificationsRead, markNotificationRead } from "../../lib/api";
import type { AppNotification } from "../../types";

const { Sider, Header, Content } = Layout;
const { Title, Text } = Typography;

const mainNavItems: MenuProps["items"] = [
  { key: "/platforms/xhs/dashboard", icon: <DashboardOutlined />, label: <span data-page-route="/platforms/xhs/dashboard">总览</span> },
  { key: "/platforms/xhs/messages", icon: <MessageOutlined />, label: <span data-page-route="/platforms/xhs/messages">私信监听</span> },
  { key: "/platforms/xhs/chat", icon: <MessageOutlined />, label: <span data-page-route="/platforms/xhs/chat">私信聊天</span> },
  { key: "/platforms/xhs/comments", icon: <BellOutlined />, label: <span data-page-route="/platforms/xhs/comments">评论监听</span> },
  { key: "/platforms/xhs/proactive", icon: <ThunderboltOutlined />, label: <span data-page-route="/platforms/xhs/proactive">主动互动</span> },
  { key: "/platforms/xhs/accounts", icon: <SafetyCertificateOutlined />, label: <span data-page-route="/platforms/xhs/accounts">账号矩阵</span> },
  { key: "/platforms/xhs/discovery", icon: <SearchOutlined />, label: <span data-page-route="/platforms/xhs/discovery">笔记发现</span> },
  { key: "/platforms/xhs/crawler", icon: <CloudDownloadOutlined />, label: <span data-page-route="/platforms/xhs/crawler">数据抓取</span> },
  { key: "/platforms/xhs/keywords", icon: <KeyOutlined />, label: <span data-page-route="/platforms/xhs/keywords">关键词组</span> },
  { key: "/platforms/xhs/analytics", icon: <BarChartOutlined />, label: <span data-page-route="/platforms/xhs/analytics">数据洞察</span> },
  { key: "/platforms/xhs/benchmarks", icon: <AimOutlined />, label: <span data-page-route="/platforms/xhs/benchmarks">竞品监控</span> },
  { key: "/platforms/xhs/image-studio", icon: <StarOutlined />, label: <span data-page-route="/platforms/xhs/image-studio">图片工坊</span> },
  { key: "/platforms/xhs/video-studio", icon: <VideoCameraOutlined />, label: <span data-page-route="/platforms/xhs/video-studio">视频工坊</span> },
  { key: "/platforms/xhs/library", icon: <DatabaseOutlined />, label: <span data-page-route="/platforms/xhs/library">内容库</span> },
  { key: "/platforms/xhs/drafts", icon: <FileTextOutlined />, label: <span data-page-route="/platforms/xhs/drafts">草稿工坊</span> },
  { key: "/platforms/xhs/publish", icon: <SendOutlined />, label: <span data-page-route="/platforms/xhs/publish">发布中心</span> },
  { key: "/platforms/xhs/auto-ops", icon: <ThunderboltOutlined />, label: <span data-page-route="/platforms/xhs/auto-ops">自动运营</span> },
];

const footerNavItems: MenuProps["items"] = [
  { key: "/platform-select", icon: <AppstoreOutlined />, label: "运营工作台" },
  { key: "/tasks", icon: <ScheduleOutlined />, label: <span data-page-route="/tasks">任务中心</span> },
  { key: "/models", icon: <RobotOutlined />, label: <span data-page-route="/models">模型配置</span> },
];

function prefetchNavigation(event: { target: EventTarget | null }) {
  if (!(event.target instanceof Element)) return;
  const item = event.target.closest(".ant-menu-item");
  const target = item?.querySelector<HTMLElement>("[data-page-route]")?.dataset.pageRoute;
  if (target) preloadRoute(target);
}

const editedTabs = new Set<string>();

function CachedPageBoundary({ children }: { children: ReactNode }) {
  const { active } = useKeepAliveContext();
  const context = useContext(UNSAFE_LocationContext);
  const savedLocation = useRef(context);
  if (active) savedLocation.current = context;
  const element = useRef<HTMLDivElement>(null);
  const position = useRef({ x: 0, y: 0, scrollers: [] as Array<[Element, number, number]> });
  useLayoutEffect(() => {
    if (!active) return;
    const saved = position.current;
    for (const [node, x, y] of saved.scrollers) node.scrollTo(x, y);
    window.scrollTo(saved.x, saved.y);
    return () => {
      element.current?.querySelectorAll<HTMLMediaElement>('video,audio').forEach(media => media.pause());
      position.current = { x: window.scrollX, y: window.scrollY,
        scrollers: [...(element.current?.querySelectorAll('*') || [])]
          .filter(node => node.scrollTop || node.scrollLeft).map(node => [node, node.scrollLeft, node.scrollTop]) };
    };
  }, [active]);
  return <UNSAFE_LocationContext.Provider value={savedLocation.current}><div ref={element} onInput={() => editedTabs.add(savedLocation.current.location.pathname)} onChange={() => editedTabs.add(savedLocation.current.location.pathname)}>
    <Suspense fallback={<div role="status" style={{ padding: 32 }}><Spin /> 正在加载页面…</div>}>{children}</Suspense>
  </div></UNSAFE_LocationContext.Provider>;
}

function levelColor(level: string): string {
  if (level === "error") return "#ef4444";
  if (level === "warning") return "#eab308";
  return "#666";
}

export function AppShell() {
  const auth = useAuth();
  const { mode: themeMode, toggle: toggleTheme } = useThemeMode();
  const navigate = useNavigate();
  const location = useLocation();
  const aliveRef = useKeepAliveRef();
  useEffect(() => { editedTabs.clear(); tabLocations.current.clear(); }, [auth.user?.id]);
  useEffect(() => {
    const changed = () => {
      const keys = aliveRef.current?.getCacheNodes().map(node => node.cacheKey)
        .filter(key => key !== location.pathname && !editedTabs.has(key)) || [];
      if (keys.length) void aliveRef.current?.destroy(keys);
    };
    window.addEventListener('xhs-workspace-data-changed', changed);
    return () => window.removeEventListener('xhs-workspace-data-changed', changed);
  }, [aliveRef, location.pathname]);
  const tabLocations = useRef(new Map<string, string>());
  useEffect(() => { tabLocations.current.set(location.pathname, location.pathname + location.search); }, [location]);
  const navigateTab = (path: string) => navigate(tabLocations.current.get(path) || path);
  const [collapsed, setCollapsed] = useState(false);
  const [mobileMenu, setMobileMenu] = useState(false);
  const screens = Grid.useBreakpoint();
  const mobile = screens.md === false;
  const [notifications, setNotifications] = useState<AppNotification[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);

  const loadNotifications = useCallback(async () => {
    try {
      const res = await fetchNotifications({ page_size: 20 });
      setNotifications(res.items);
      setUnreadCount(res.items.filter((n) => !n.read).length);
    } catch { /* silent */ }
  }, []);

  useEffect(() => {
    void loadNotifications();
    const timer = setInterval(() => void loadNotifications(), 30_000);
    return () => clearInterval(timer);
  }, [loadNotifications]);

  const handleMarkRead = async (id: number) => { await markNotificationRead(id); void loadNotifications(); };
  const handleMarkAllRead = async () => { await markAllNotificationsRead(); void loadNotifications(); };
  const handleMenuClick: MenuProps["onClick"] = ({ key }) => { preloadRoute(key); startTransition(() => navigateTab(key)); };
  const selectedKeys = [location.pathname];

  const notificationDropdownContent = (
    <div style={{ width: 360, background: "var(--ui-surface,#1f1f1f)", borderRadius: 8, border: "1px solid var(--ui-line,#303030)", overflow: "hidden" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "12px 16px", borderBottom: "1px solid var(--ui-line,#303030)" }}>
        <Text strong style={{ fontSize: 14 }}>通知</Text>
        {unreadCount > 0 && <Button type="link" size="small" onClick={() => void handleMarkAllRead()}>全部已读</Button>}
      </div>
      <div style={{ maxHeight: 400, overflowY: "auto" }}>
        {notifications.length === 0 ? (
          <div style={{ padding: "32px 16px", textAlign: "center", color: "var(--ui-muted)" }}>暂无通知</div>
        ) : (
          <List
            dataSource={notifications}
            renderItem={(n) => (
              <List.Item key={n.id} style={{ padding: "10px 16px", cursor: n.read ? "default" : "pointer", background: n.read ? "transparent" : "rgba(22,104,220,0.06)", borderBottom: "1px solid var(--ui-line,#262626)" }} onClick={() => !n.read && void handleMarkRead(n.id)}>
                <List.Item.Meta
                  avatar={<span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%", background: levelColor(n.level), marginTop: 6 }} />}
                  title={<Text style={{ fontSize: 13 }}>{n.title}</Text>}
                  description={<div>{n.body && <Text type="secondary" style={{ fontSize: 12, display: "block" }}>{n.body}</Text>}<Text type="secondary" style={{ fontSize: 11 }}>{new Date(n.created_at).toLocaleString("zh-CN")}</Text></div>}
                />
              </List.Item>
            )}
          />
        )}
      </div>
    </div>
  );

  const siderWidth = mobile ? 0 : collapsed ? 64 : 220;

  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider
        collapsed={mobile || collapsed}
        width={220}
        collapsedWidth={mobile ? 0 : 64}
        theme={themeMode}
        trigger={null}
        style={{
          height: "100vh",
          position: "fixed",
          left: 0,
          top: 0,
          bottom: 0,
          borderRight: "1px solid var(--ui-line,#303030)",
          overflow: "hidden",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
          {/* Logo */}
          <div
            style={{ padding: collapsed ? "14px 0" : "14px 16px", display: "flex", alignItems: "center", justifyContent: collapsed ? "center" : "space-between", borderBottom: "1px solid var(--ui-line,#303030)", flexShrink: 0, cursor: "pointer" }}
            onClick={() => navigate("/platform-select")}
          >
            <Space align="center" size={collapsed ? 0 : 8}>
              <img src="/logo.jpg" alt="Logo" style={{ width: 28, height: 28, borderRadius: 6, objectFit: "cover", flexShrink: 0 }} />
              {!collapsed && <span style={{ fontWeight: 600, fontSize: 14, color: "var(--ui-muted)" }}>Spider XHS</span>}
            </Space>
            {!collapsed && <Button type="text" size="small" icon={<MenuFoldOutlined />} onClick={(e) => { e.stopPropagation(); setCollapsed(true); }} style={{ color: "var(--ui-muted)" }} />}
          </div>
          {collapsed && (
            <div style={{ textAlign: "center", padding: "6px 0", borderBottom: "1px solid var(--ui-line,#262626)", flexShrink: 0 }}>
              <Button type="text" size="small" icon={<MenuUnfoldOutlined />} onClick={() => setCollapsed(false)} style={{ color: "var(--ui-muted)" }} />
            </div>
          )}

          {/* Main nav — scrollable */}
          <div style={{ flex: 1, overflowY: "auto", overflowX: "hidden" }}>
            <Menu theme={themeMode} mode="inline" selectedKeys={selectedKeys} onClick={handleMenuClick} items={mainNavItems} onMouseOver={prefetchNavigation} onFocus={prefetchNavigation} style={{ borderRight: 0 }} />
          </div>

          {/* Footer — pinned to bottom */}
          <div style={{ flexShrink: 0, borderTop: "1px solid var(--ui-line,#303030)" }}>
            <Menu theme={themeMode} mode="inline" selectedKeys={selectedKeys} onClick={handleMenuClick} items={footerNavItems} onMouseOver={prefetchNavigation} onFocus={prefetchNavigation} style={{ borderRight: 0 }} />
            <div style={{ padding: collapsed ? "8px 0" : "8px 16px", borderTop: "1px solid var(--ui-line,#262626)", display: "flex", alignItems: "center", gap: 8, justifyContent: collapsed ? "center" : "flex-start" }}>
              <Avatar size={22} icon={<UserOutlined />} style={{ background: "#1668dc", flexShrink: 0, fontSize: 11 }}>{(auth.user?.username ?? "U")[0].toUpperCase()}</Avatar>
              {!collapsed && (
                <>
                  <Text type="secondary" ellipsis style={{ fontSize: 12, flex: 1, lineHeight: "22px" }}>{auth.user?.username ?? "用户"}</Text>
                  <Button aria-label="退出登录" title="退出登录 / 切换用户" type="text" icon={<LogoutOutlined />} onClick={() => void auth.logout()} size="small" style={{ color: "var(--ui-muted)", flexShrink: 0 }} />
                </>
              )}
            </div>
          </div>
        </div>
      </Sider>

      <Drawer title="工作台导航" placement="left" open={mobile && mobileMenu} onClose={() => setMobileMenu(false)} styles={{ body: { padding: 0 } }}>
        <Menu mode="inline" selectedKeys={selectedKeys} items={[...(mainNavItems || []), ...(footerNavItems || [])]} onMouseOver={prefetchNavigation} onFocus={prefetchNavigation} onClick={({ key }) => { preloadRoute(key); startTransition(() => navigateTab(key)); setMobileMenu(false); }} />
        <Button type="text" icon={<LogoutOutlined />} onClick={() => void auth.logout()}>退出登录</Button>
      </Drawer>
      <Layout style={{ marginLeft: siderWidth, minWidth: 0, transition: "margin-left 0.2s" }}>
        <Header style={{ padding: mobile ? "0 16px" : "0 24px", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, borderBottom: "1px solid var(--ui-line,#303030)", height: 48, lineHeight: "48px" }}>
          <Space size={4} align="center">
            {mobile && <Button aria-label="打开导航" type="text" icon={<MenuUnfoldOutlined />} onClick={() => setMobileMenu(true)} />}
            <Button
              type="text"
              href="https://d.58nft.net:9461/platform-select"
              icon={<AppstoreOutlined />}
              aria-label="返回运营工作台"
              title="返回运营工作台"
              style={{ paddingInline: mobile ? 8 : 12, whiteSpace: "nowrap" }}
            >运营工作台</Button>
          </Space>
          <Space size={12} align="center">
            <Button
              type="text"
              icon={themeMode === "dark" ? <SunOutlined style={{ fontSize: 16 }} /> : <MoonOutlined style={{ fontSize: 16 }} />}
              onClick={toggleTheme}
              title={themeMode === "dark" ? "切换为浅色模式" : "切换为暗色模式"}
              style={{ display: "flex", alignItems: "center", justifyContent: "center" }}
            />
            <Dropdown dropdownRender={() => notificationDropdownContent} trigger={["click"]} placement="bottomRight">
              <Badge count={unreadCount} size="small" offset={[-2, 2]}>
                <Button type="text" icon={<BellOutlined style={{ fontSize: 16 }} />} style={{ display: "flex", alignItems: "center", justifyContent: "center" }} />
              </Badge>
            </Dropdown>
            <Avatar size={28} style={{ background: "#1668dc", fontSize: 12, cursor: "default" }}>{(auth.user?.username ?? "U")[0].toUpperCase()}</Avatar>
          </Space>
        </Header>
        <Content style={{ padding: mobile ? 16 : 24, minHeight: "calc(100vh - 48px)", overflow: "auto" }}>
          <KeepAliveRouteOutlet
            key={'workspace-user-' + auth.user?.id}
            wrapperComponent={CachedPageBoundary}
            aliveRef={aliveRef}
            max={24}
            activeCacheKey={location.pathname}
            include={[/^\/platforms\/xhs\//, /^\/(tasks|models)$/]}
          />
        </Content>
      </Layout>
    </Layout>
  );
}

export function PageHeader({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: ReactNode }) {
  return (
    <Row justify="space-between" align="top" style={{ marginBottom: 24 }}>
      <Col>
        <Text type="secondary" style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: 1 }}>{eyebrow}</Text>
        <Title level={3} style={{ margin: "4px 0 4px" }}>{title}</Title>
        <Text type="secondary">{description}</Text>
      </Col>
      {action && <Col>{action}</Col>}
    </Row>
  );
}
