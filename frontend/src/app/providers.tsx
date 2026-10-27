import { App, ConfigProvider, theme } from "antd";
import zhCN from "antd/locale/zh_CN";
import { createContext, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";

type ThemeMode = "dark" | "light";

const ThemeContext = createContext<{ mode: ThemeMode; toggle: () => void }>({
  mode: "dark",
  toggle: () => {},
});

export function useThemeMode() {
  return useContext(ThemeContext);
}

const darkTokens = {
  colorLink: "#85baff", colorLinkHover: "#b8d8ff", colorLinkActive: "#85baff",
  colorTextSecondary: "#aaaaaa", colorTextTertiary: "#aaaaaa", colorTextPlaceholder: "#aaaaaa",
  colorPrimary: "#1668dc",
  colorBgBase: "#141414",
  colorBgContainer: "#1f1f1f",
  colorBgElevated: "#262626",
  colorBorder: "#303030",
  colorBorderSecondary: "#262626",
  borderRadius: 6,
  fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
};

const lightTokens = {
  colorLink: "#1257b5", colorLinkHover: "#1668dc", colorLinkActive: "#1257b5",
  colorTextSecondary: "#666666", colorTextTertiary: "#666666", colorTextPlaceholder: "#737373",
  colorPrimary: "#1668dc",
  colorBgBase: "#ffffff",
  colorBgContainer: "#ffffff",
  colorBgElevated: "#ffffff",
  colorBorder: "#e8e8e8",
  colorBorderSecondary: "#f0f0f0",
  borderRadius: 6,
  fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
};

const darkComponents = {
  Layout: { siderBg: "#141414", headerBg: "#141414", bodyBg: "#0a0a0a" },
  Menu: { darkItemBg: "#141414", darkSubMenuItemBg: "#141414", darkItemSelectedBg: "#1668dc20", darkItemSelectedColor: "#85baff" },
  Card: { colorBgContainer: "#1a1a1a" },
  Table: { colorBgContainer: "#1a1a1a", headerBg: "#1f1f1f" },
};

const lightComponents = {
  Layout: { siderBg: "#ffffff", headerBg: "#ffffff", bodyBg: "#f5f5f5" },
  Menu: { itemSelectedColor: "#1257b5", itemSelectedBg: "#eaf2ff", itemHoverColor: "#1257b5" },
  Card: {},
  Table: {},
};

type AppProvidersProps = { children: ReactNode };

export function AppProviders({ children }: AppProvidersProps) {
  const [mode, setMode] = useState<ThemeMode>(() => {
    try { const saved = localStorage.getItem("workspace-theme") || localStorage.getItem("theme-mode"); return saved === "light" ? "light" : "dark"; }
    catch { return "dark"; }
  });

  useEffect(() => {
    try { localStorage.setItem("theme-mode", mode); localStorage.setItem("workspace-theme", mode); } catch { /* Optional preference storage. */ }
    document.documentElement.dataset.workspaceTheme = mode;
    document.documentElement.style.colorScheme = mode;
    document.body.style.background = mode === "dark" ? "#0a0a0a" : "#f5f5f5";
  }, [mode]);

  useEffect(() => {
    const changed = (event: StorageEvent) => {
      if (event.key === 'theme-mode' || event.key === 'workspace-theme') setMode(event.newValue === 'light' ? 'light' : 'dark');
    };
    window.addEventListener('storage', changed);
    return () => window.removeEventListener('storage', changed);
  }, []);

  const toggle = () => setMode((m) => (m === "dark" ? "light" : "dark"));
  const isDark = mode === "dark";

  return (
    <ThemeContext.Provider value={{ mode, toggle }}>
      <ConfigProvider
        locale={zhCN}
        theme={{
          algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
          token: isDark ? darkTokens : lightTokens,
          components: isDark ? darkComponents : lightComponents,
        }}
      >
        <App>{children}</App>
      </ConfigProvider>
    </ThemeContext.Provider>
  );
}
