import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Card, CardBody, CardHeader, Input, Button } from "@heroui/react";
import { motion } from "framer-motion";
import { KeyRound } from "lucide-react";
import { api } from "../api/client";

export default function LoginPage() {
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const handleLogin = async () => {
    if (!token.trim()) return;
    setLoading(true);
    setError("");
    try {
      await api.auth(token.trim());
      navigate("/", { replace: true });
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "认证失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex items-center justify-center min-h-screen bg-background">
      <motion.div
        initial={{ opacity: 0, y: 20, scale: 0.95 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.5, type: "spring", stiffness: 120, damping: 20 }}
        className="w-[420px] max-w-full px-4"
      >
        <Card className="backdrop-blur-xl backdrop-saturate-150 border border-white/10 shadow-2xl bg-content1/80">
          <CardHeader className="flex flex-col items-center gap-2 pt-10 pb-2">
            <div className="flex items-center gap-2">
              <div className="h-6 w-1.5 bg-primary rounded-full" />
              <h1 className="text-2xl font-bold tracking-wide">YuKiKo</h1>
            </div>
            <p className="text-sm text-default-400">WebUI 管理面板</p>
          </CardHeader>
          <CardBody className="gap-5 px-8 pb-8 pt-4">
            <Input
              type="password"
              placeholder="请输入访问令牌"
              size="lg"
              radius="lg"
              startContent={<KeyRound size={18} className="text-default-400" />}
              classNames={{
                inputWrapper: [
                  "shadow-lg",
                  "bg-default-100/70",
                  "dark:bg-default/60",
                  "backdrop-blur-xl",
                  "backdrop-saturate-200",
                  "hover:bg-default-100/90",
                  "dark:hover:bg-default/70",
                  "group-data-[focus=true]:bg-default-100/50",
                ],
              }}
              value={token}
              onValueChange={setToken}
              onKeyDown={(e) => e.key === "Enter" && handleLogin()}
              isInvalid={!!error}
              errorMessage={error}
            />
            <p className="text-center text-xs text-default-400">
              请输入 .env 中配置的 WEBUI_TOKEN
            </p>
            <Button
              color="primary"
              variant="shadow"
              radius="full"
              size="lg"
              fullWidth
              className="mt-4 text-base font-medium py-6"
              isLoading={loading}
              onPress={handleLogin}
            >
              登录
            </Button>
          </CardBody>
        </Card>
      </motion.div>
    </div>
  );
}
