import { Routes, Route, Navigate } from "react-router-dom";
import { api } from "./api/client";
import AppShell from "./components/app-shell";
import LoginPage from "./pages/login";
import SetupPage from "./pages/setup";
import DashboardPage from "./pages/dashboard";
import ConfigPage from "./pages/config";
import PromptsPage from "./pages/prompts";
import LogsPage from "./pages/logs";
import DatabasePage from "./pages/database";
import MemoryPage from "./pages/memory";
import CookiesPage from "./pages/cookies";
import PluginsPage from "./pages/plugins";
import ChatPage from "./pages/chat";

function AuthGuard({ children }: { children: React.ReactNode }) {
  const token = api.getToken();
  if (!token) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={
          <AuthGuard>
            <AppShell />
          </AuthGuard>
        }
      >
        <Route index element={<DashboardPage />} />
        <Route path="config" element={<ConfigPage />} />
        <Route path="prompts" element={<PromptsPage />} />
        <Route path="logs" element={<LogsPage />} />
        <Route path="database" element={<DatabasePage />} />
        <Route path="chat" element={<ChatPage />} />
        <Route path="memory" element={<MemoryPage />} />
        <Route path="cookies" element={<CookiesPage />} />
        <Route path="plugins" element={<PluginsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
