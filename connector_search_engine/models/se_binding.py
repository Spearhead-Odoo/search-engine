# Copyright 2013 Akretion (http://www.akretion.com)
# Copyright 2021 Camptocamp (http://www.camptocamp.com)
# Simone Orsi <simone.orsi@camptocamp.com>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import json
import logging
import sys
from typing import Any, Dict, Iterator

from odoo import _, api, fields, models, tools
from odoo.exceptions import ValidationError

from odoo.addons.queue_job.job import identity_exact

from ..tools.json_comparer import compare_json

_logger = logging.getLogger(__name__)


class SeBinding(models.Model):
    _name = "se.binding"
    _description = "Search Engine Record"

    backend_id = fields.Many2one(
        "se.backend", related="index_id.backend_id", string="Search Engine Backend"
    )
    index_id = fields.Many2one(
        "se.index",
        string="Index",
        required=True,
        # TODO: shall we use 'restrict' here to preserve existing data?
        ondelete="cascade",
    )
    state = fields.Selection(
        [
            ("to_recompute", "To recompute"),
            ("recomputing", "Recomputing"),
            ("to_export", "To export"),
            ("exporting", "Exporting"),
            ("done", "Done"),
            ("invalid_data", "Invalid Data"),
            ("to_delete", "To Delete"),
            ("deleting", "Deleting"),
        ],
        default="to_recompute",
        compute="_compute_state",
        store=True,
        readonly=False,
    )
    date_recomputed = fields.Datetime(readonly=True)
    date_synchronized = fields.Datetime(readonly=True)
    data = fields.Json()
    data_display = fields.Text(
        compute="_compute_data_display",
        help="Include this in debug mode to be able to inspect index data.",
    )
    validation_error = fields.Text()
    res_id = fields.Many2oneReference(
        string="Record ID",
        help="ID of the target record in the database",
        model_field="res_model",
    )
    res_model = fields.Selection(selection=lambda s: s._get_indexable_model_selection())

    _sql_constraints = [
        (
            "item_uniq_per_index",
            "unique(res_id, res_model, index_id)",
            _("A record can only be bind one time per index !"),
        ),
    ]

    @tools.ormcache()
    @api.model
    def _get_indexable_model_selection(self):
        return [
            (model, self.env[model]._description)
            for model in self.env
            if (
                hasattr(self.env[model], "_se_indexable")
                and not self.env[model]._abstract
                and not self.env[model]._transient
            )
        ]

    def _compute_state(self):
        # TODO add the logic for invalidating the state
        # automatically based on the exporter
        pass

    @property
    def record(self) -> models.Model:
        if len(set(self.mapped("res_model"))) > 1:
            raise ValueError("All record must have the same model")
        return self.env[self[0].res_model].browse(self.mapped("res_id")).exists()

    @api.depends("data")
    def _compute_data_display(self):
        for rec in self:
            rec.data_display = json.dumps(rec.data, sort_keys=True, indent=4)

    def get_export_data(self) -> Dict[str, Any]:
        """Public method to retrieve export data."""
        return self.data

    def _batch(self, size: int) -> Iterator["SeBinding"]:
        """Return an iterator on the records in batch of size."""
        for i in range(0, len(self), size):
            yield self[i : i + size]

    def _jobify_get_job_description(self, processing_count: int) -> str:
        """Return a human readable description to be used when creating a job."""
        # We check if we are currently handling an exception in order
        # to change the job description.
        (current_exception, current_exception_value, __) = sys.exc_info()
        if current_exception:
            return _(
                "Batch of %(processing_count)s items for %(model_name)s. "
                "Previous batch raised %(exception_name)s: %(exception_value)s."
            ) % dict(
                processing_count=processing_count,
                model_name=self._name,
                exception_name=current_exception.__name__,
                exception_value=current_exception_value,
            )
        else:
            return _(
                "Batch task of %(processing_count)s for "
                "recomputing %(model_name)s json"
            ) % dict(
                processing_count=processing_count,
                model_name=self._name,
            )

    def jobify_recompute_json(self, force_export: bool = False):
        """Create one job per record to recompute the json."""
        # The job creation with tracking is very costly. So disable it.
        for binding in self.with_context(tracking_disable=True):
            description = _(
                "Recompute %(name)s json and check if need update", name=self._name
            )
            binding.with_delay(
                description=description, identity_key=identity_exact
            ).recompute_json(force_export=force_export)

    def recompute_json(self, force_export: bool = False):
        """ "Compute index record data as JSON."""
        # `sudo` because the recomputation can be triggered from everywhere
        # (eg: an update of a product in the stock) and is not granted
        # that the user triggering it has access to all required records
        # (eg: se.backend or related records needed to compute index values).
        # All in all, this is safe because the index data should always
        # be the same no matter the access rights of the user triggering this.

        for record in self.sudo():
            if not record.record:
                record.state = "to_delete"
                _logger.error(
                    "There is something wrong, the record do not exists "
                    "flag the binding to be deleted"
                )
                continue
            index = record.index_id
            # force the lang if needed
            if index.lang_id:
                record = record.with_context(lang=index.lang_id.code)

            old_data = record.data
            record.data = index.model_serializer.serialize(record.record)
            record.date_recomputed = fields.Datetime.now()
            try:
                index.json_validator.validate(record.data)
            except ValidationError as e:
                record.state = "invalid_data"
                record.validation_error = str(e)
            else:
                if force_export or not compare_json(old_data or {}, record.data or {}):
                    record.state = "to_export"
                else:
                    record.state = "done"
                record.validation_error = ""

    def export_record(self) -> str:
        self.ensure_one()
        adapter = self.index_id.se_adapter
        adapter.index([record.get_export_data() for record in self])
        self.state = "done"
        return "Exported ids : {}".format(self.ids)

    def delete_record(self) -> str:
        self.ensure_one()
        adapter = self.index_id.se_adapter
        record_ids = self.mapped("res_id")
        adapter.delete(record_ids)
        self.unlink()
        return "Deleted ids : {}".format(record_ids)
