// Approved, not shipped yet: the last row of a backend's model list on Settings · Models, so adding
// starts where the list is read. It opens the shipped add dialog (`BackendModelPickerDialog`).
import { Plus } from 'lucide-react';

import { Button } from '@/components/ui/button';

import { useDraft } from './draft';

/** The last row of a backend's model list: opens the add dialog for that backend. */
export function ModelListAddRow() {
  const d = useDraft();
  return (
    <Button type="button" variant="outline" size="sm" className="h-10 w-full rounded-md text-[12px] font-semibold">
      <Plus aria-hidden="true" />
      {d('添加模型', 'Add model')}
    </Button>
  );
}
