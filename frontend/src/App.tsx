import { BrowserRouter, Navigate, Route, Routes } from "react-router";
import { AuthProvider } from "./auth/AuthContext";
import { useAuth } from "./auth/useAuth";
import { Layout } from "./components/Layout";
import { CatalogsPage } from "./pages/CatalogsPage";
import { LoginPage } from "./pages/LoginPage";
import { SettingsPage } from "./pages/SettingsPage";

function RequireAuth() {
  const { user, loading } = useAuth();
  if (loading) {
    return (
      <div className="container">
        <p className="subtitle">Loading...</p>
      </div>
    );
  }
  if (!user) {
    return <Navigate to="/login" replace />;
  }
  return <Layout />;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth />}>
        <Route index element={<Navigate to="/settings" replace />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/catalogs" element={<CatalogsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/settings" replace />} />
    </Routes>
  );
}

export function App() {
  return (
    <BrowserRouter basename="/app">
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  );
}
