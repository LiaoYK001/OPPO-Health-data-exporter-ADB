import {
  AnchorHTMLAttributes,
  createContext,
  MouseEvent,
  ReactNode,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

interface RouterValue {
  pathname: string;
  navigate: (to: string) => void;
}

const RouterContext = createContext<RouterValue | null>(null);

export function RouterProvider({ children }: { children: ReactNode }) {
  const [pathname, setPathname] = useState(window.location.pathname);
  useEffect(() => {
    const update = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  const value = useMemo(
    () => ({
      pathname,
      navigate: (to: string) => {
        if (to === window.location.pathname) return;
        window.history.pushState({}, "", to);
        setPathname(to);
        window.scrollTo({ top: 0, behavior: "instant" });
      },
    }),
    [pathname],
  );
  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>;
}

export function useLocation() {
  const value = useContext(RouterContext);
  if (!value) throw new Error("RouterProvider is missing");
  return value;
}

export function Link({
  to,
  onClick,
  ...props
}: AnchorHTMLAttributes<HTMLAnchorElement> & { to: string }) {
  const { navigate } = useLocation();
  const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event);
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    event.preventDefault();
    navigate(to);
  };
  return <a href={to} onClick={handleClick} {...props} />;
}

export function NavLink({
  to,
  end = false,
  className = "",
  ...props
}: AnchorHTMLAttributes<HTMLAnchorElement> & { to: string; end?: boolean }) {
  const { pathname } = useLocation();
  const active = end ? pathname === to : pathname === to || pathname.startsWith(`${to}/`);
  return (
    <Link
      to={to}
      className={`${typeof className === "string" ? className : ""}${active ? " active" : ""}`.trim()}
      {...props}
    />
  );
}
