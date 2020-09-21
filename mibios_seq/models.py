from itertools import chain

from Bio import SeqIO
from django.db import models
from django.db.transaction import atomic

from omics.shared import MothurShared
from mibios.dataset import UserDataError
from mibios.models import Manager, PublishManager, Model, ParentModel, QuerySet
from mibios.utils import getLogger


log = getLogger(__name__)


class Sample(ParentModel):
    """
    Parent model for samples

    This is the multi-table-inheritance parent that other apps should use to
    interface with sequencing data.  There are no fields declared here besides
    the usual auto-primary-key and history.
    """
    pass


class SeqNote(Model):
    name = models.CharField(max_length=100, unique=True)
    text = models.TextField(max_length=5000, blank=True)


class Sequencing(Model):
    MOCK = 'mock'
    WATER = 'water'
    BLANK = 'blank'
    PLATE = 'plate'
    OTHER = 'other'
    CONTROL_CHOICES = (
        (MOCK, MOCK),
        (WATER, WATER),
        (BLANK, BLANK),
        (PLATE, PLATE),
        (OTHER, OTHER),
    )
    name = models.CharField(max_length=100, unique=True)
    sample = models.ForeignKey(Sample, on_delete=models.CASCADE,
                               blank=True, null=True)
    control = models.CharField(max_length=50, choices=CONTROL_CHOICES,
                               blank=True)
    r1_file = models.CharField(max_length=300, unique=True, blank=True,
                               null=True)
    r2_file = models.CharField(max_length=300, unique=True, blank=True,
                               null=True)
    note = models.ManyToManyField(SeqNote, blank=True)
    run = models.ForeignKey('SequencingRun', on_delete=models.CASCADE,
                            blank=True, null=True)
    plate = models.PositiveSmallIntegerField(blank=True, null=True)
    plate_position = models.CharField(max_length=10, blank=True)
    snumber = models.PositiveSmallIntegerField(blank=True, null=True)

    class Meta:
        unique_together = (
            ('run', 'snumber'),
            ('run', 'plate', 'plate_position'),
        )
        ordering = ['name']

    @classmethod
    def parse_control(cls, txt):
        """
        Coerce text into available control choices
        """
        choice = txt.strip().lower()
        if choice:
            for i in (j[0] for j in cls.CONTROL_CHOICES):
                if i in choice:
                    return i
            return cls.OTHER
        else:
            return ''


class SequencingRun(Model):
    serial = models.CharField(max_length=50)
    number = models.PositiveSmallIntegerField()
    path = models.CharField(max_length=2000, blank=True)

    class Meta:
        unique_together = ('serial', 'number')
        ordering = ['serial', 'number']

    @Model.natural.getter
    def natural(self):
        return '{}-{}'.format(self.serial, self.number)

    @classmethod
    def natural_lookup(cls, value):
        s, n = value.split('-')
        return dict(serial=s, number=int(n))


class Strain(Model):
    asv = models.ForeignKey('ASV', on_delete=models.SET_NULL, blank=True,
                            null=True)


class AbundanceQuerySet(QuerySet):
    def as_shared(self):
        """
        Make mothur-shared table

        Note: without label/numOtus columns

        Returns a pandas DataFrame.  Assumes, that the QuerySet is filtered to
        counts from a single analysis project but this is not checked.  If the
        assumption is violated, then the pivot operation will probably raise a:

            "ValueError: Index contains duplicate entries, cannot reshape"

        Missing counts are inserted as zero, mirroring the skipping of zeros at
        import.
        """
        df = (
            self
            .as_dataframe('asv', 'sequencing', 'count', natural=True)
            .pivot(index='sequencing', columns='asv', values='count')
        )
        df.fillna(value=0, inplace=True)  # pivot introduced NaNs
        return df

    def as_shared_values_list(self):
        """
        Make mothur-shared table

        Returns an iterator over tuple rows, first row is the header.  This is
        intended to support data export.
        """
        sh = self.as_shared()
        header = ['Group'] + list(sh.columns)
        recs = sh.itertuples(index=True, name=False)
        return chain([header], recs)


class Abundance(Model):
    history = None
    name = models.CharField(
        max_length=50, verbose_name='project internal id',
        default='',
        blank=True,
        help_text='project specific ASV/OTU identifier',
    )
    count = models.PositiveIntegerField(
        help_text='absolute abundance',
        editable=False,
    )
    project = models.ForeignKey(
        'AnalysisProject',
        on_delete=models.CASCADE,
        editable=False,
    )
    sequencing = models.ForeignKey(
        Sequencing,
        on_delete=models.CASCADE,
        editable=False,
    )
    asv = models.ForeignKey(
        'ASV',
        on_delete=models.CASCADE,
        editable=False,
    )

    class Meta:
        unique_together = (
            # one count per project / ASV / sample
            ('name', 'sequencing', 'project'),
            ('asv', 'sequencing', 'project'),
        )

    objects = Manager.from_queryset(AbundanceQuerySet)()
    published = PublishManager.from_queryset(AbundanceQuerySet)()

    def __str__(self):
        return super().__str__() + f' |{self.count}|'

    @classmethod
    def from_file(cls, file, project, fasta=None):
        """
        Load abundance data from shared file

        :param file fasta: Fasta file object

        If a fasta file is given, then the input does not need to use proper
        ASV numbers.  Instead ASVs are identified by sequence and ASV objects
        are created as needed.  Obviously, the OTU/ASV/sequence names in shared
        and fasta files must correspond.
        """
        sh = MothurShared(file, verbose=False, threads=1)
        with atomic():
            if fasta:
                fasta_result = ASV.from_fasta(fasta)

            sequencings = Sequencing.published.in_bulk(field_name='name')
            asvs = ASV.published.in_bulk(field_name='number')  # get numbered

            if fasta:
                asvs.update(fasta_result['irregular'])

            skipped, zeros = 0, 0
            objs = []
            for (sample, asv), count in sh.counts.stack().items():
                if count == 0:
                    # don't store zeros
                    zeros += 1
                    continue

                if sample not in sequencings:
                    # ok to skip, e.g. non-public
                    skipped += 1
                    continue

                try:
                    asv_key = ASV.natural_lookup(asv)['number']
                except ValueError:
                    asv_key = asv

                try:
                    asv_obj = asvs[asv_key]
                except KeyError:
                    raise UserDataError(f'Unknown ASV: {asv}')

                objs.append(cls(
                    name=asv,
                    count=count,
                    project=project,
                    sequencing=sequencings[sample],
                    asv=asv_obj,
                ))

            cls.published.bulk_create(objs)
        return dict(count=len(objs), zeros=zeros, skipped=skipped)


class AnalysisProject(Model):
    name = models.CharField(max_length=100, unique=True)
    asv = models.ManyToManyField('ASV', through=Abundance, editable=False)
    description = models.TextField(blank=True)

    @classmethod
    def get_fields(cls, with_m2m=False, **kwargs):
        # Prevent abundance from being displayed, too much data
        return super().get_fields(with_m2m=False, **kwargs)


class ASV(Model):
    PREFIX = 'ASV'
    NUM_WIDTH = 5

    number = models.PositiveIntegerField(null=True, blank=True, unique=True)
    taxon = models.ForeignKey('Taxonomy', on_delete=models.SET_NULL,
                              blank=True, null=True)
    sequence = models.CharField(
        max_length=300,  # > length of 16S V4
        unique=True,
        editable=False,
    )

    class Meta:
        ordering = ('number',)

    def __str__(self):
        return str(self.natural)
        s = str(self.natural)
        if self.taxon:
            genus, _, species = self.taxon.name.partition(' ')
            if species:
                genus = genus.lstrip('[')[0].upper() + '.'
                s += ' ' + genus + ' ' + species
            else:
                s += ' ' + str(self.taxon)

        return s

    @property
    def name(self):
        return str(self.natural)

    @Model.natural.getter
    def natural(self):
        if self.number:
            return self.PREFIX + '{}'.format(self.number).zfill(self.NUM_WIDTH)
        else:
            return self.pk

    @classmethod
    def natural_lookup(cls, value):
        """
        Given e.g. ASV00023, return dict(number=23)

        Raises ValueError if value does not parse
        """
        # FIXME: require casefolded ASV prefix?
        return dict(number=int(value[len(cls.PREFIX):]))

    @classmethod
    @atomic
    def from_fasta(cls, file):
        """
        Import from given fasta file

        For fasta headers that do not have ASV00000 type id the returned
        'irregular' dict will map the irregular names to the corresponding ASV
        instance.  Re-loading un-numbered sequences with a proper ASV number
        will get the number updated.  If a sequence already has a number and it
        doesn't match the number in the file an IntegrityError is raised.
        """
        added, updated, total = 0, 0, 0
        irregular = {}
        for i in SeqIO.parse(file, 'fasta'):
            try:
                numkw = cls.natural_lookup(i.id)
            except ValueError:
                numkw = dict()
                number = None
            else:
                number = numkw['number']

            obj, new = cls.objects.get_or_create(sequence=i.seq, **numkw)

            if new:
                added += 1
            else:
                if obj.number is None and number:
                    # update number
                    obj.number = number
                    obj.save()
                    updated += 1

            if number is None:
                # save map from irregular sequence id to ASV
                irregular[i.id] = obj

            total += 1

        return dict(total=total, new=added, updated=updated,
                    irregular=irregular)


class Taxonomy(Model):
    taxid = models.PositiveIntegerField(
        unique=True,
        verbose_name='NCBI taxonomy id',
    )
    name = models.CharField(
        max_length=300,
        unique=True,
        verbose_name='taxonomic name',
    )

    def __str__(self):
        return '{} ({})'.format(self.name, self.taxid)

    @classmethod
    @atomic
    def from_blast_top1(cls, file):
        """
        Import from blast-result-top-1 format file

        The supported file format is a tab-delimited text file with header row,
        column 1 are ASV accessions, columns 5 and 6 are NCBI taxids and names,
        and if there are ties then column 7 are the least-common NCBI taxids
        and column 8 are the corresponding taxon names

        The taxonomy for existing ASV records is imported, everything else is
        ignored.
        """
        asvs = {i.number: i for i in ASV.objects.select_related()}
        file.readline()
        updated, total = 0, 0
        for line in file:
            try:
                total += 1
                row = line.rstrip('\n').split('\t')
                asv, taxid, name, lctaxid, lcname = row[0], *row[4:]

                if lcname and lcname:
                    name = lcname
                    taxid = lctaxid

                taxid = int(taxid)
                num = ASV.natural_lookup(asv)['number']

                if num not in asvs:
                    # ASV not in database
                    continue

                taxon, _ = cls.objects.get_or_create(taxid=taxid, name=name)
                if asvs[num].taxon == taxon:
                    del asvs[num]
                else:
                    asvs[num].taxon = taxon
                    updated += 1
            except Exception as e:
                raise RuntimeError(
                    f'error loading file: {file} at line {total}: {row}'
                ) from e

        ASV.objects.bulk_update(asvs.values(), ['taxon'])
        return dict(total=total, update=updated)
