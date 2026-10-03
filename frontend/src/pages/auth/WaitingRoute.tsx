import { Spinner } from "../dashboard/ui";
import WaitingApproval from "./WaitingApproval";
import { useAuth } from "./auth";

/** /waiting: pending and disabled accounts land here; the component redirects everyone else. */
export default function WaitingRoute() {
  const { loading } = useAuth();
  if (loading) return <Spinner label="Checking your session" />;
  return <WaitingApproval />;
}
