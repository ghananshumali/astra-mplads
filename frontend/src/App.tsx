import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./components/shell/AppShell";
import Cases from "./pages/Cases";
import DataSource from "./pages/DataSource";
import Geography from "./pages/Geography";
import Network from "./pages/Network";
import Overview from "./pages/Overview";
import Pipeline from "./pages/Pipeline";
import { AuthorityProvider } from "./state/AuthorityContext";

const qc = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

export default function App() {
  return (
    <QueryClientProvider client={qc}>
      <BrowserRouter>
        <AuthorityProvider>
          <AppShell>
            <Routes>
              <Route path="/" element={<Overview />} />
              <Route path="/cases" element={<Cases />} />
              <Route path="/cases/:flagId" element={<Cases />} />
              <Route path="/geography" element={<Geography />} />
              <Route path="/network" element={<Network />} />
              <Route path="/pipeline" element={<Pipeline />} />
              <Route path="/data" element={<DataSource />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </AppShell>
        </AuthorityProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
