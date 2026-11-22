import { NavLink, Outlet } from "react-router";
import { useAuth } from "../auth/useAuth";
import styles from "./Layout.module.css";

export function Layout() {
  const { user, logout } = useAuth();

  return (
    <div className="container">
      <header className={styles.header}>
        <h1 className={styles.title}>Curio</h1>
        {user && (
          <div className={styles.userBar}>
            <span>{user.username}</span>
            <button className="btn" type="button" onClick={() => void logout()}>
              Sign out
            </button>
          </div>
        )}
      </header>

      <main>
        <nav className={styles.tabs} aria-label="Control plane sections">
          <NavLink
            to="/settings"
            className={({ isActive }) =>
              isActive ? `${styles.tab} ${styles.tabActive}` : styles.tab
            }
          >
            Settings
          </NavLink>
          <NavLink
            to="/catalogs"
            className={({ isActive }) =>
              isActive ? `${styles.tab} ${styles.tabActive}` : styles.tab
            }
          >
            Catalogs
          </NavLink>
        </nav>
        <Outlet />
      </main>
    </div>
  );
}
