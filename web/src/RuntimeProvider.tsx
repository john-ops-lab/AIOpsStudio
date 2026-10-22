import { CopilotKit } from "@copilotkit/react-core";
import type { ReactNode } from "react";
export default function RuntimeProvider({ children }: { children: ReactNode }) {
  return (
    <CopilotKit
      runtimeUrl="/api/copilotkit"
      agent="easyops"
      showDevConsole={false}
    >
      {children}
    </CopilotKit>
  );
}
