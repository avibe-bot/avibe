import * as React from "react";
import { useTranslation } from "react-i18next";

import type { SupplyGap } from "./types";

export const GuardGapList: React.FC<{ gaps: SupplyGap[] }> = ({ gaps }) => {
  const { t, i18n } = useTranslation();
  if (gaps.length === 0) return null;
  return (
    <>
      <div className="model-hub-guard-label">
        <p>{t("settings.models.guard.gap.label")}</p>
        <span>
          {t("settings.models.gateway.modelCount", { count: gaps.length })}
        </span>
      </div>
      <div className="model-hub-guard-list">
        {gaps.map((gap) => (
          <div
            key={`${gap.backend}:${gap.model_id}`}
            className="model-hub-guard-hop"
          >
            <span className="min-w-0 flex-1">
              <strong>
                {t("settings.models.guard.gap.subject", {
                  backend: t(`settings.models.backends.${gap.backend}`, {
                    defaultValue: gap.backend,
                  }),
                  menuModel: gap.model_id,
                })}
              </strong>
              {gap.agents.length > 0 && (
                <span>
                  {t("settings.models.guard.gap.agents", {
                    agents: gap.agents.join(
                      i18n.language.startsWith("zh") ? "、" : ", ",
                    ),
                  })}
                </span>
              )}
            </span>
          </div>
        ))}
      </div>
    </>
  );
};
