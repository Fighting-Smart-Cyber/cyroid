// frontend/src/components/files/CreateFileModal.tsx
/**
 * Create a file in a content tree from a template.
 *
 * The templates are documentation: for most users the "Training Scenario" one is the only
 * example of a scenario they will ever see. It used to emit fields the scenario parser does not
 * read -- duration_hours, time_offset, a required_roles list of mappings -- so a file created
 * from the product's own template was rejected by the product's own API, and until wave 2 of the
 * audit one such file took the whole scenario list down with it. The template below is the shape
 * `services/scenario_filesystem.py` actually reads, and a backend test parses this very string.
 */
import { useEffect, useMemo, useState } from 'react';
import { isAxiosError } from 'axios';
import { FileCode, Loader2 } from 'lucide-react';
import clsx from 'clsx';
import { filesApi } from '../../services/api';
import { toast } from '../../stores/toastStore';
import { useFeature } from '../../stores/capabilitiesStore';
import { Modal, ModalBody, ModalFooter } from '../common/Modal';

interface CreateFileModalProps {
  isOpen: boolean;
  basePath: string;
  onClose: () => void;
  onCreated: (path: string) => void;
}

type FileType = 'dockerfile' | 'yaml' | 'shell' | 'markdown' | 'json' | 'text';

interface FileTemplate {
  type: FileType;
  label: string;
  defaultName: string;
  content: string;
}

/** The one template the scenario tree exists for; named so it can be put first there. */
const SCENARIO_TEMPLATE_LABEL = 'Training Scenario';

// SCENARIO_TEMPLATE_START -- backend/tests/unit/test_scenario_template.py reads the string
// between these markers and parses it. Keep the markers.
const SCENARIO_TEMPLATE = `# A training scenario: a timeline of injects, each addressed to a role.
# Roles are bound to the range's machines at the moment the scenario is applied,
# so a scenario is written once and reused by any range that can fill its roles.

seed_id: new-scenario
name: New Training Scenario
description: What this scenario trains, and what the learner is expected to do.

category: blue-team        # red-team | blue-team | insider-threat
difficulty: intermediate   # beginner | intermediate | advanced
duration_minutes: 120

# Role names, as plain text. Every target_role below must appear in this list.
required_roles:
  - defender
  - target

# Each event becomes one inject. sequence orders them; delay_minutes is the
# offset from the start of the exercise. An event with no actions is a timed
# instruction the exercise controller reads out and marks off.
events:
  - sequence: 1
    delay_minutes: 0
    title: Suspicious authentication burst
    description: >-
      Failed logons against the target climb sharply. The defender is expected
      to identify the source and say how they would contain it.
    target_role: defender
    actions: []

  - sequence: 2
    delay_minutes: 30
    title: Service account used out of hours
    description: >-
      A service account authenticates outside its normal window. The defender
      reports whether this is related to the earlier burst.
    target_role: defender
    actions: []

  - sequence: 3
    delay_minutes: 75
    title: Data staged for exfiltration
    description: >-
      An archive appears in a temporary directory on the target. The exercise
      ends when the defender has reported it and proposed a containment step.
    target_role: target
    actions: []
`;
// SCENARIO_TEMPLATE_END

const FILE_TEMPLATES: FileTemplate[] = [
  {
    type: 'dockerfile',
    label: 'Dockerfile',
    defaultName: 'Dockerfile',
    content: `FROM ubuntu:22.04

LABEL maintainer="your-email@example.com"
LABEL description="Description of this image"

# Install dependencies
RUN apt-get update && apt-get install -y \\
    package1 \\
    package2 \\
    && rm -rf /var/lib/apt/lists/*

# Copy configuration files
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Set working directory
WORKDIR /app

# Expose ports
EXPOSE 22 80

# Set entrypoint
ENTRYPOINT ["/entrypoint.sh"]
`,
  },
  {
    type: 'shell',
    label: 'Shell Script',
    defaultName: 'script.sh',
    content: `#!/bin/bash
set -e

# Description: What this script does
# Usage: ./script.sh [options]

main() {
    echo "Starting..."
    # Your code here
}

main "$@"
`,
  },
  {
    type: 'yaml',
    label: 'YAML Config',
    defaultName: 'config.yaml',
    content: `# Configuration file
name: example
version: "1.0"

settings:
  enabled: true
  timeout: 30

items:
  - name: item1
    value: 100
  - name: item2
    value: 200
`,
  },
  {
    type: 'yaml',
    label: SCENARIO_TEMPLATE_LABEL,
    defaultName: 'scenario.yaml',
    content: SCENARIO_TEMPLATE,
  },
  {
    type: 'json',
    label: 'JSON Config',
    defaultName: 'config.json',
    content: `{
  "name": "example",
  "version": "1.0.0",
  "settings": {
    "enabled": true,
    "timeout": 30
  },
  "items": [
    { "name": "item1", "value": 100 },
    { "name": "item2", "value": 200 }
  ]
}
`,
  },
  {
    type: 'markdown',
    label: 'Markdown (README)',
    defaultName: 'README.md',
    content: `# Project Name

Brief description of this project.

## Features

- Feature 1
- Feature 2

## Usage

\`\`\`bash
./script.sh
\`\`\`

## Configuration

Describe configuration options here.

## License

MIT
`,
  },
  {
    type: 'text',
    label: 'Empty Text File',
    defaultName: 'file.txt',
    content: '',
  },
];

/** Whatever the server said went wrong, or a fallback that at least names the operation. */
function reason(err: unknown, fallback: string): string {
  if (isAxiosError(err)) {
    const detail = err.response?.data?.detail;
    if (typeof detail === 'string' && detail) return detail;
    if (Array.isArray(detail)) {
      const messages = detail
        .map((d) => (d && typeof d === 'object' && typeof d.msg === 'string' ? d.msg : null))
        .filter((m): m is string => m !== null);
      if (messages.length) return messages.join('; ');
    }
    if (!err.response) return `${fallback} — no answer from the server`;
  }
  if (err instanceof Error && err.message) return err.message;
  return fallback;
}

export function CreateFileModal({
  isOpen,
  basePath,
  onClose,
  onCreated,
}: CreateFileModalProps) {
  // The image trees, and the build that reads a Dockerfile out of one, are Era A. On a
  // Kubernetes install nothing in the product builds an image, so offering the template here
  // would be offering a file with nowhere to go.
  const buildsImages = useFeature('image_cache');
  const inScenarioTree = basePath.split('/')[0] === 'scenarios';

  const templates = useMemo(() => {
    const visible = FILE_TEMPLATES.filter((t) => t.type !== 'dockerfile' || buildsImages);
    if (!inScenarioTree) return visible;
    // In the scenario tree the scenario template is the one a user came for.
    return [
      ...visible.filter((t) => t.label === SCENARIO_TEMPLATE_LABEL),
      ...visible.filter((t) => t.label !== SCENARIO_TEMPLATE_LABEL),
    ];
  }, [buildsImages, inScenarioTree]);

  const [selectedLabel, setSelectedLabel] = useState(templates[0].label);
  const [fileName, setFileName] = useState(templates[0].defaultName);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // The modal stays mounted between openings, and the list it selects from can change under it
  // when the install's capabilities arrive. Starting each opening from the first visible
  // template keeps the selected template and the suggested file name describing each other.
  useEffect(() => {
    if (!isOpen) return;
    setSelectedLabel(templates[0].label);
    setFileName(templates[0].defaultName);
    setError(null);
  }, [isOpen, templates]);

  const selectedTemplate =
    templates.find((t) => t.label === selectedLabel) ?? templates[0];

  const handleTemplateSelect = (template: FileTemplate) => {
    setSelectedLabel(template.label);
    setFileName(template.defaultName);
    setError(null);
  };

  const handleCreate = async () => {
    if (!fileName.trim()) {
      setError('File name is required');
      return;
    }

    // Basic validation
    if (fileName.includes('/') || fileName.includes('\\')) {
      setError('File name cannot contain path separators');
      return;
    }

    const fullPath = `${basePath}/${fileName}`.replace(/\/+/g, '/');

    setCreating(true);
    setError(null);
    try {
      await filesApi.createFile(fullPath, selectedTemplate.content);
      toast.success(`Created ${fileName}`);
      onCreated(fullPath);
    } catch (err: unknown) {
      const detail = reason(err, 'Failed to create file');
      setError(detail);
      toast.error(detail);
    } finally {
      setCreating(false);
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Create New File"
      size="lg"
      description="Select a file template and enter a file name to create a new file"
    >
      <ModalBody>
        {/* Template Selection */}
        <div className="mb-4">
          <label className="block text-sm font-medium text-gray-700 mb-2">
            File Template
          </label>
          <div className="grid grid-cols-2 gap-2">
            {templates.map((template) => (
              <button
                key={template.label}
                onClick={() => handleTemplateSelect(template)}
                className={clsx(
                  'flex items-center px-3 py-2 text-sm rounded-lg border transition-colors text-left',
                  selectedTemplate.label === template.label
                    ? 'border-primary-500 bg-primary-50 text-primary-700'
                    : 'border-gray-200 hover:bg-gray-50 text-gray-700'
                )}
              >
                <FileCode className="h-4 w-4 mr-2 flex-shrink-0" />
                {template.label}
              </button>
            ))}
          </div>
        </div>

        {/* File Name */}
        <div className="mb-4">
          <label className="block text-sm font-medium text-gray-700 mb-1">
            File Name
          </label>
          <input
            type="text"
            value={fileName}
            onChange={(e) => {
              setFileName(e.target.value);
              setError(null);
            }}
            className={clsx(
              'w-full px-3 py-2 border rounded-lg text-sm',
              error
                ? 'border-red-300 focus:ring-red-500 focus:border-red-500'
                : 'border-gray-300 focus:ring-primary-500 focus:border-primary-500'
            )}
            placeholder="filename.ext"
          />
          {error && <p className="mt-1 text-sm text-red-600">{error}</p>}
        </div>

        {/* Path Preview */}
        <div className="p-3 bg-gray-50 rounded-lg">
          <p className="text-xs text-gray-500 mb-1">Will create:</p>
          <code className="text-sm text-gray-700">
            {basePath}/{fileName}
          </code>
        </div>
      </ModalBody>

      <ModalFooter>
        <button
          onClick={onClose}
          className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50"
        >
          Cancel
        </button>
        <button
          onClick={handleCreate}
          disabled={creating || !fileName.trim()}
          className={clsx(
            'inline-flex items-center px-4 py-2 text-sm font-medium rounded-md',
            creating || !fileName.trim()
              ? 'bg-gray-100 text-gray-400 cursor-not-allowed'
              : 'bg-primary-600 text-white hover:bg-primary-700'
          )}
        >
          {creating && <Loader2 className="h-4 w-4 mr-2 animate-spin" />}
          Create File
        </button>
      </ModalFooter>
    </Modal>
  );
}
