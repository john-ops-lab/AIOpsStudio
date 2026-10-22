import { createContext, useContext } from "react";
import type { User } from "../types";
export const IdentityContext = createContext<User | null>(null);
export function useIdentity() {
  const user = useContext(IdentityContext);
  return { user, isAdmin: user?.role === "admin" };
}
